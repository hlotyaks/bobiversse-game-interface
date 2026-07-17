const catalogElement = document.querySelector('#catalog');
const capacityElement = document.querySelector('#capacity');
const noticeElement = document.querySelector('#notice');
const dialog = document.querySelector('#confirm-dialog');
const template = document.querySelector('#game-template');
let catalog = [];
let instances = [];
let capacity = null;

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
  renderCapacity();
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
      const crashLoop = record?.status?.crash_loop === true;
      const ports = record?.instance?.ports?.map((port) => `${port.protocol.toUpperCase()} ${port.host}`).join(' · ') || 'Ports assigned when registered';
      const usage = record?.status?.memory_current_mib !== undefined ? `<br>Observed: ${record.status.memory_current_mib} MiB memory · ${Math.round(Number(record.status.cpu_usage_nsec || 0) / 1e9)}s CPU time` : '';
      const backup = record?.status?.backup || {};
      const backupDetail = record ? backup.latest_timestamp
        ? `<br>Backup: ${backup.latest_timestamp} · ${backup.verification_passed ? 'verified' : 'verification failed'}`
        : '<br>Backup: no verified automated backup yet' : '';
      const crashDetail = crashLoop ? `<br><strong>Crash loop:</strong> ${record.status.last_failure_reason || 'manual retry required'}` : '';
      row.innerHTML = `<div class="instance-row"><span class="instance-name">${instanceId}</span><span class="badge ${crashLoop || state === 'failed' ? 'failed' : state === 'active' ? 'healthy' : 'idle'}">${crashLoop ? 'CRASH LOOP' : state}</span></div><p class="meta">${record ? `${ports}<br>Unit: ${record.status.unit}${usage}${backupDetail}${crashDetail}` : 'This slot is not registered yet.'}</p>`;
      const actions = document.createElement('div');
      actions.className = 'actions';
      const registering = !record;
      const starting = ['active', 'activating', 'deactivating'].includes(state);
      const capacityReason = capacityBlockReason(game, record, state);
      const capacityAllowed = !capacityReason;
      if (!capacityAllowed) row.querySelector('.meta').textContent += ` Resource policy: ${capacityReason}.`;
      actions.append(button(registering ? 'Register slot' : crashLoop ? 'Manual retry' : 'Start', registering ? 'secondary' : '', () => registering ? register(game.template_id, instanceId) : lifecycle('start', game.template_id, instanceId), !game.enabled || starting || !capacityAllowed));
      if (record && !crashLoop) actions.append(button('Restart', 'secondary', () => lifecycle('restart', game.template_id, instanceId), !game.enabled || starting));
      row.append(actions);
      list.append(row);
    });
    catalogElement.append(card);
  });
}

function capacityBlockReason(game, record, state) {
  if (!capacity || state === 'active') return '';
  const resources = record?.instance?.resources || game.resources || {};
  const projected = capacity.running_reservation || {};
  const limits = capacity.limits || {};
  const reserve = capacity.host_safety_reserve || {};
  const cpu = Number(projected.cpu_cores || 0) + Number(resources.cpu_cores || 0);
  const memory = Number(projected.memory_mib || 0) + Number(resources.memory_mib || 0);
  const disk = Number(projected.disk_gib || 0) + Number(resources.disk_gib || 0) + Number(reserve.disk_gib || 0);
  if (cpu > Number(limits.cpu_cores || 0)) return `CPU reservation would be ${cpu} of ${limits.cpu_cores} cores`;
  if (memory > Number(limits.memory_mib || 0)) return `memory reservation would be ${memory} of ${limits.memory_mib} MiB`;
  const unavailableDisk = (capacity.disk || []).find((entry) => entry.error || Number(entry.available_gib) < disk);
  if (unavailableDisk) return unavailableDisk.error ? `disk path ${unavailableDisk.path} is unavailable` : `disk at ${unavailableDisk.path} needs ${disk} GiB free`;
  if (Number(capacity.host_swap_free_mib || 0) < Number(reserve.swap_free_mib || 0)) return `swap free is below ${reserve.swap_free_mib} MiB`;
  return '';
}

function renderCapacity() {
  capacityElement.replaceChildren();
  if (!capacity) return;
  const reservation = capacity.running_reservation || {};
  const limits = capacity.limits || {};
  const disk = (capacity.disk || [])[0] || {};
  const values = [
    ['CPU reservation', `${reservation.cpu_cores || 0} / ${limits.cpu_cores || 0} cores`, 'Running instances'],
    ['Memory reservation', `${reservation.memory_mib || 0} / ${limits.memory_mib || 0} MiB`, `${capacity.host_memory_available_mib || 0} MiB host available`],
    ['Disk headroom', `${disk.available_gib ?? '?'} GiB free`, `${capacity.host_safety_reserve?.disk_gib || 0} GiB safety reserve`],
  ];
  values.forEach(([label, value, detail]) => {
    const card = document.createElement('div');
    card.className = 'capacity-item';
    card.innerHTML = `<span>${label}</span><strong>${value}</strong><small>${detail}</small>`;
    capacityElement.append(card);
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
    [catalog, instances, capacity] = await Promise.all([request('/api/catalog'), request('/api/instances'), request('/api/capacity')]);
    render();
  } catch (error) {
    catalogElement.innerHTML = '<p class="empty">Unable to reach the game interface.</p>';
    showNotice(error.message, true);
  }
}

document.querySelector('#refresh').addEventListener('click', load);
load();
