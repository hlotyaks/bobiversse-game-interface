const catalogElement = document.querySelector('#catalog');
const noticeElement = document.querySelector('#notice');
const dialog = document.querySelector('#confirm-dialog');
const template = document.querySelector('#game-template');
let catalog = [];
let instances = [];

function showNotice(message, error = false) {
  noticeElement.textContent = message;
  noticeElement.hidden = false;
  noticeElement.className = `notice${error ? ' error' : ''}`;
}

async function request(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const body = await response.json().catch(() => ({ error: 'Unexpected response from interface.' }));
  if (!response.ok) throw new Error(body.error || 'Request failed.');
  return body.result;
}

function instanceFor(templateId, instanceId) {
  return instances.find(({ instance }) => instance.template_id === templateId && instance.instance_id === instanceId);
}

function render() {
  catalogElement.replaceChildren();
  if (!catalog.length) {
    catalogElement.innerHTML = '<p class="empty">The game catalog is unavailable.</p>';
    return;
  }
  catalog.forEach((game) => {
    const card = template.content.cloneNode(true);
    card.querySelector('.game-id').textContent = game.template_id;
    card.querySelector('.game-name').textContent = game.display_name;
    card.querySelector('.game-description').textContent = game.description;
    const badge = card.querySelector('.badge');
    badge.textContent = game.enabled ? 'Available' : 'Disabled';
    badge.className = `badge ${game.enabled ? 'enabled' : 'failed'}`;
    const list = card.querySelector('.instance-list');
    game.instance_ids.forEach((instanceId) => {
      const record = instanceFor(game.template_id, instanceId);
      const row = document.createElement('div');
      row.className = 'instance';
      const state = record?.status?.active_state || (record ? 'pending' : 'not registered');
      const ports = record?.instance?.ports?.map((port) => `${port.protocol.toUpperCase()} ${port.host}`).join(' · ') || 'Ports assigned when registered';
      row.innerHTML = `<div class="instance-row"><span class="instance-name">${instanceId}</span><span class="badge ${state === 'active' ? 'healthy' : state === 'failed' ? 'failed' : 'idle'}">${state}</span></div><p class="meta">${record ? `${ports}<br>Unit: ${record.status.unit}` : 'This slot is not registered yet.'}</p>`;
      const actions = document.createElement('div');
      actions.className = 'actions';
      const registering = !record;
      const starting = ['active', 'activating', 'deactivating'].includes(state);
      actions.append(button(registering ? 'Register slot' : 'Start', registering ? 'secondary' : '', () => registering ? register(game.template_id, instanceId) : lifecycle('start', game.template_id, instanceId), !game.enabled || starting));
      if (record) actions.append(button('Restart', 'secondary', () => lifecycle('restart', game.template_id, instanceId), !game.enabled || starting));
      row.append(actions);
      list.append(row);
    });
    catalogElement.append(card);
  });
}

function button(label, style, handler, disabled) {
  const element = document.createElement('button');
  element.className = `button ${style}`;
  element.type = 'button';
  element.textContent = label;
  element.disabled = disabled;
  element.addEventListener('click', handler);
  return element;
}

function confirm(title, text) {
  return new Promise((resolve) => {
    document.querySelector('#dialog-title').textContent = title;
    document.querySelector('#dialog-text').textContent = text;
    dialog.showModal();
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
  });
}

async function register(templateId, instanceId) {
  if (!await confirm('Register this game slot?', 'Registration reserves this catalog-defined slot. It does not create a world or start a server.')) return;
  try {
    await request('/api/instances', { method: 'POST', body: JSON.stringify({ template_id: templateId, instance_id: instanceId }) });
    showNotice(`${templateId}:${instanceId} is registered and awaits root-reviewed provisioning.`);
    await load();
  } catch (error) { showNotice(error.message, true); }
}

async function lifecycle(action, templateId, instanceId) {
  const warning = action === 'restart' ? 'Restarting disconnects active players.' : 'Starting consumes shared host capacity.';
  if (!await confirm(`${action === 'restart' ? 'Restart' : 'Start'} ${templateId}:${instanceId}?`, warning)) return;
  try {
    const operation = await request(`/api/actions/${action}`, { method: 'POST', body: JSON.stringify({ template_id: templateId, instance_id: instanceId }) });
    showNotice(operation.state === 'already-running' ? 'The instance is already running.' : `Operation ${operation.operation_id} is ${operation.state}.`);
    if (operation.operation_id) watchOperation(operation.operation_id);
    await load();
  } catch (error) { showNotice(error.message, true); }
}

async function watchOperation(operationId) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    try {
      const operation = await request(`/api/operations/${operationId}`);
      if (['healthy', 'failed'].includes(operation.state)) {
        showNotice(operation.message || `Operation finished: ${operation.state}.`, operation.state === 'failed');
        await load();
        return;
      }
    } catch (error) { showNotice(error.message, true); return; }
  }
  showNotice('Operation is still in progress; refresh status shortly.');
}

async function load() {
  try {
    [catalog, instances] = await Promise.all([request('/api/catalog'), request('/api/instances')]);
    render();
  } catch (error) {
    catalogElement.innerHTML = '<p class="empty">Unable to reach the game interface.</p>';
    showNotice(error.message, true);
  }
}

document.querySelector('#refresh').addEventListener('click', load);
load();
