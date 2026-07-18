const catalogElement = document.querySelector('#catalog');
const capacityElement = document.querySelector('#capacity');
const noticeElement = document.querySelector('#notice');
const dialog = document.querySelector('#confirm-dialog');
const gameRequestDialog = document.querySelector('#game-request-dialog');
const gameRequestForm = document.querySelector('#game-request-form');
const gameRequestError = document.querySelector('#game-request-error');
const addGameButton = document.querySelector('#add-game');
const template = document.querySelector('#game-template');
const refreshStatusElement = document.querySelector('#refresh-status');
const IDLE_REFRESH_INTERVAL_MS = 10000;
const OPERATION_REFRESH_INTERVAL_MS = 2000;
const OPERATION_MAX_ATTEMPTS = 60;
let catalog = [];
let instances = [];
let capacity = null;
let gameRequestPolicy = { allowed: false };
let refreshTimer = null;
let refreshInFlight = false;
const trackedOperations = new Map();
const consoleEntries = new Map();

function showNotice(message, error = false) {
  noticeElement.textContent = message;
  noticeElement.hidden = false;
  noticeElement.className = `notice${error ? ' error' : ''}`;
}

function showGameRequestError(message = '') {
  gameRequestError.textContent = message;
  gameRequestError.hidden = !message;
}

function downloadGameRequest(requestData) {
  const slug = requestData.requested_slug;
  const content = `${JSON.stringify(requestData, null, 2)}\n`;
  const blob = new Blob([content], { type: 'application/json' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `game-request-${slug}-${requestData.steam_app_id}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
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

function consoleKey(templateId, instanceId) {
  return `${templateId}:${instanceId}`;
}

function trackedOperationFor(templateId, instanceId) {
  return [...trackedOperations.values()].find((operation) => operation.templateId === templateId && operation.instanceId === instanceId);
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
      const address = connectionAddress(game, record);
      if (address) {
        const connection = document.createElement('div');
        connection.className = 'connection';
        const label = document.createElement('span');
        label.textContent = 'Connect';
        const value = document.createElement('code');
        value.textContent = address;
        connection.append(label, value, button('Copy address', 'secondary', () => copyConnection(address)));
        row.querySelector('.meta').after(connection);
      }
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
      const key = consoleKey(game.template_id, instanceId);
      const operation = trackedOperationFor(game.template_id, instanceId);
      const entry = consoleEntries.get(key);
      if (record) row.append(logConsole(game.template_id, instanceId, entry, Boolean(operation)));
      list.append(row);
    });
    catalogElement.append(card);
  });
}

function connectionAddress(game, record) {
  const hostname = game.connection?.hostname;
  const ports = record?.instance?.ports || [];
  const gamePort = ports
    .filter((port) => port.protocol === 'udp' && Number.isInteger(Number(port.host)))
    .map((port) => Number(port.host))
    .sort((left, right) => left - right)[0];
  return typeof hostname === 'string' && hostname && gamePort ? `${hostname}:${gamePort}` : '';
}

async function copyConnection(address) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard access is unavailable');
    await navigator.clipboard.writeText(address);
    showNotice(`Copied connection address: ${address}`);
  } catch (_error) {
    showNotice(`Copy is unavailable. Use: ${address}`, true);
  }
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

function logConsole(templateId, instanceId, entry = {}, inProgress = false) {
  const details = document.createElement('details');
  details.className = 'console';
  details.open = entry.expanded ?? inProgress;
  const summary = document.createElement('summary');
  summary.textContent = 'Server logs';
  const status = document.createElement('p');
  status.className = 'console-status';
  status.textContent = entry.loading ? 'Loading logs…' : entry.error || (inProgress ? 'Startup progress from the systemd journal' : 'Recent systemd journal output');
  const output = document.createElement('pre');
  output.className = 'console-output';
  output.textContent = Array.isArray(entry.lines) && entry.lines.length ? entry.lines.join('\n') : 'No journal output is available yet.';
  details.append(summary, status, output);
  details.addEventListener('toggle', () => {
    const key = consoleKey(templateId, instanceId);
    const current = consoleEntries.get(key) || {};
    consoleEntries.set(key, { ...current, expanded: details.open });
    if (details.open && !current.loading && !Array.isArray(current.lines)) loadLogs(templateId, instanceId);
  });
  return details;
}

async function loadLogs(templateId, instanceId) {
  const key = consoleKey(templateId, instanceId);
  const previous = consoleEntries.get(key) || {};
  consoleEntries.set(key, { ...previous, expanded: true, loading: true, error: '' });
  render();
  try {
    const logs = await request(`/api/logs?template_id=${encodeURIComponent(templateId)}&instance_id=${encodeURIComponent(instanceId)}&tail=100`);
    consoleEntries.set(key, { lines: Array.isArray(logs.lines) ? logs.lines : [], expanded: true, loading: false, error: '' });
  } catch (_error) {
    consoleEntries.set(key, { ...previous, expanded: true, loading: false, error: 'Logs are unavailable right now.' });
  }
  render();
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
    await refresh();
  } catch (error) { showNotice(error.message, true); }
}

async function lifecycle(action, templateId, instanceId) {
  const warning = action === 'restart' ? 'Restarting disconnects active players.' : 'Starting consumes shared host capacity.';
  if (!await confirm(`${action === 'restart' ? 'Restart' : 'Start'} ${templateId}:${instanceId}?`, warning)) return;
  try {
    const operation = await request(`/api/actions/${action}`, { method: 'POST', body: JSON.stringify({ template_id: templateId, instance_id: instanceId }) });
    showNotice(operation.state === 'already-running' ? 'The instance is already running.' : `Operation ${operation.operation_id} is ${operation.state}.`);
    if (operation.operation_id) trackOperation(operation);
    await refresh();
  } catch (error) { showNotice(error.message, true); }
}

function openGameRequestDialog() {
  showGameRequestError();
  gameRequestForm.reset();
  gameRequestDialog.showModal();
  document.querySelector('#steam-url').focus();
}

async function submitGameRequest(event) {
  event.preventDefault();
  showGameRequestError();
  const form = new FormData(gameRequestForm);
  const payload = {
    steam_url: String(form.get('steam_url') || ''),
    requested_slug: String(form.get('requested_slug') || ''),
    purpose: String(form.get('purpose') || ''),
  };
  try {
    const requestData = await request('/api/game-requests', { method: 'POST', body: JSON.stringify(payload) });
    downloadGameRequest(requestData);
    gameRequestDialog.close();
    showNotice(`Downloaded review request for ${requestData.requested_slug}. Run the operator metadata tool outside this host, then open a pull request.`);
  } catch (error) {
    showGameRequestError(error.message);
  }
}

function updateRefreshStatus(message) {
  refreshStatusElement.textContent = message;
}

function clearRefreshTimer() {
  if (refreshTimer !== null) clearTimeout(refreshTimer);
  refreshTimer = null;
}

function scheduleRefresh(delay) {
  clearRefreshTimer();
  if (document.hidden) {
    updateRefreshStatus('Automatic updates paused while this tab is hidden.');
    return;
  }
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    refresh();
  }, delay);
}

function trackOperation(operation) {
  trackedOperations.set(operation.operation_id, {
    attempts: 0,
    templateId: operation.template_id,
    instanceId: operation.instance_id,
  });
  scheduleRefresh(OPERATION_REFRESH_INTERVAL_MS);
}

async function watchOperations() {
  const operations = [...trackedOperations.entries()];
  await Promise.all(operations.map(async ([operationId, tracked]) => {
    const attempts = tracked.attempts + 1;
    try {
      const operation = await request(`/api/operations/${operationId}`);
      if (['healthy', 'failed'].includes(operation.state)) {
        trackedOperations.delete(operationId);
        const key = consoleKey(tracked.templateId, tracked.instanceId);
        if (operation.state === 'healthy') consoleEntries.delete(key);
        else {
          try {
            const logs = await request(`/api/logs?template_id=${encodeURIComponent(tracked.templateId)}&instance_id=${encodeURIComponent(tracked.instanceId)}&tail=25`);
            consoleEntries.set(key, { lines: Array.isArray(logs.lines) ? logs.lines : [], expanded: true, loading: false, error: '' });
          } catch (_error) {
            consoleEntries.set(key, { expanded: true, loading: false, error: 'Final startup logs are unavailable right now.' });
          }
        }
        showNotice(operation.message || `Operation finished: ${operation.state}.`, operation.state === 'failed');
        return;
      }
      if (attempts >= OPERATION_MAX_ATTEMPTS) {
        trackedOperations.delete(operationId);
        showNotice('Operation is still in progress; refresh status shortly.');
        return;
      }
      const key = consoleKey(tracked.templateId, tracked.instanceId);
      try {
        const logs = await request(`/api/logs?template_id=${encodeURIComponent(tracked.templateId)}&instance_id=${encodeURIComponent(tracked.instanceId)}&tail=25`);
        const previous = consoleEntries.get(key) || {};
        consoleEntries.set(key, { lines: Array.isArray(logs.lines) ? logs.lines : [], expanded: previous.expanded ?? true, loading: false, error: '' });
      } catch (_error) {
        const previous = consoleEntries.get(key) || {};
        consoleEntries.set(key, { ...previous, expanded: previous.expanded ?? true, loading: false, error: 'Startup logs are unavailable right now.' });
      }
      trackedOperations.set(operationId, { ...tracked, attempts });
    } catch (error) {
      trackedOperations.delete(operationId);
      showNotice(error.message, true);
    }
  }));
}

async function load() {
  [catalog, instances, capacity, gameRequestPolicy] = await Promise.all([request('/api/catalog'), request('/api/instances'), request('/api/capacity'), request('/api/game-requests/policy')]);
  addGameButton.hidden = gameRequestPolicy.allowed !== true;
  render();
}

async function refresh() {
  if (refreshInFlight || document.hidden) return;
  refreshInFlight = true;
  updateRefreshStatus('Updating status…');
  try {
    await watchOperations();
    await load();
    updateRefreshStatus(`Updated ${new Date().toLocaleTimeString()} · automatic updates every ${trackedOperations.size ? 2 : 10} seconds`);
  } catch (error) {
    catalogElement.innerHTML = '<p class="empty">Unable to reach the game interface.</p>';
    showNotice(error.message, true);
    updateRefreshStatus('Unable to update; retrying automatically.');
  } finally {
    refreshInFlight = false;
    scheduleRefresh(trackedOperations.size ? OPERATION_REFRESH_INTERVAL_MS : IDLE_REFRESH_INTERVAL_MS);
  }
}

document.querySelector('#refresh').addEventListener('click', refresh);
addGameButton.addEventListener('click', openGameRequestDialog);
document.querySelector('#game-request-cancel').addEventListener('click', () => gameRequestDialog.close());
gameRequestForm.addEventListener('submit', submitGameRequest);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearRefreshTimer();
    updateRefreshStatus('Automatic updates paused while this tab is hidden.');
  } else {
    refresh();
  }
});
if (document.hidden) updateRefreshStatus('Automatic updates paused while this tab is hidden.');
else refresh();
