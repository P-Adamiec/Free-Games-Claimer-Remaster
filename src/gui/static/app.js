const token = document.querySelector('meta[name="fgc-token"]').content;
const storeGrid = document.querySelector('#storeGrid');
const toast = document.querySelector('#toast');
const logoStores = new Set(['steam', 'epic', 'gog', 'ubisoft', 'aliexpress']);
let latestStatus = null;
let latestConfig = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-FGC-Token': token,
      ...(options.headers || {}),
    },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Não foi possível concluir a operação.');
  return data;
}

function showToast(message, error = false) {
  toast.textContent = message;
  toast.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = 'toast'; }, 3500);
}

function relativeTime(value) {
  if (!value) return 'Nunca';
  const date = new Date(value);
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat('pt-BR', { numeric: 'auto' });
  const units = [[86400, 'day'], [3600, 'hour'], [60, 'minute']];
  for (const [size, unit] of units) {
    if (Math.abs(seconds) >= size) return formatter.format(Math.round(seconds / size), unit);
  }
  return formatter.format(seconds, 'second');
}

function createStoreIcon(store) {
  const icon = document.createElement('span');
  icon.className = `store-icon store-${store.key}`;
  icon.setAttribute('aria-hidden', 'true');
  if (logoStores.has(store.key)) {
    const logo = document.createElement('span');
    logo.className = `store-logo logo-${store.key}`;
    icon.append(logo);
  } else {
    icon.textContent = store.badge;
  }
  return icon;
}

function createStoreRow(store, globalRunning) {
  const row = document.createElement('article');
  row.className = 'store-row';
  row.dataset.state = store.state;

  const identity = document.createElement('div');
  identity.className = 'store-identity';
  const names = document.createElement('div');
  const title = document.createElement('h3');
  title.className = 'store-name';
  title.textContent = store.name;
  const key = document.createElement('p');
  key.className = 'store-key';
  key.textContent = store.key;
  names.append(title, key);
  identity.append(createStoreIcon(store), names);

  const state = document.createElement('div');
  state.className = 'store-state';
  const marker = document.createElement('span');
  marker.className = 'state-marker';
  const message = document.createElement('span');
  message.textContent = store.message;
  state.append(marker, message);

  const lastRun = document.createElement('div');
  lastRun.className = 'last-run';
  const lastRunLabel = document.createElement('span');
  lastRunLabel.className = 'last-run-label';
  lastRunLabel.textContent = 'Última execução';
  const lastRunValue = document.createElement('strong');
  lastRunValue.className = 'last-run-value';
  lastRunValue.textContent = store.lastRun ? relativeTime(store.lastRun) : 'Nesta sessão: nunca';
  lastRun.append(lastRunLabel, lastRunValue);

  const run = document.createElement('button');
  run.className = 'button run-store';
  run.type = 'button';
  run.textContent = store.state === 'running' ? 'Executando' : 'Executar';
  run.disabled = globalRunning;
  run.addEventListener('click', () => runStores([store.key]));

  row.append(identity, state, lastRun, run);
  return row;
}

function createAddStoreRow(availableCount) {
  const button = document.createElement('button');
  button.className = 'add-store-row';
  button.id = 'addStoreButton';
  button.type = 'button';
  const symbol = document.createElement('span');
  symbol.className = 'add-symbol';
  symbol.textContent = '+';
  symbol.setAttribute('aria-hidden', 'true');
  const copy = document.createElement('span');
  copy.className = 'add-copy';
  const title = document.createElement('strong');
  title.textContent = availableCount ? 'Adicionar loja' : 'Gerenciar lojas';
  const detail = document.createElement('small');
  detail.textContent = availableCount
    ? `${availableCount} ${availableCount === 1 ? 'loja disponível' : 'lojas disponíveis'}`
    : 'Todas as lojas disponíveis estão ativas';
  copy.append(title, detail);
  button.append(symbol, copy);
  button.addEventListener('click', openStoreManager);
  return button;
}

function renderStatus(status) {
  latestStatus = status;
  const enabledStores = status.stores.filter(store => store.enabled);
  const disabledCount = status.stores.length - enabledStores.length;
  storeGrid.replaceChildren(
    ...enabledStores.map(store => createStoreRow(store, status.running)),
    createAddStoreRow(disabledCount),
  );
  document.querySelector('#activeStoreCount').textContent = String(enabledStores.length);
  document.querySelector('#lastRun').textContent = relativeTime(status.finishedAt);
  document.querySelector('#nextRun').textContent = status.schedule.nextRun
    ? relativeTime(status.schedule.nextRun)
    : 'Somente manual';
  document.querySelector('#runLabel').textContent = status.running ? 'Executando' : 'Pronto';
  document.querySelector('#runPill').classList.toggle('running', status.running);
  document.querySelector('#runAllButton').disabled = status.running || enabledStores.length === 0;
}

async function refreshStatus(silent = true) {
  try {
    renderStatus(await api('/api/status'));
  } catch (error) {
    if (!silent) showToast(error.message, true);
  }
}

async function runStores(stores = null) {
  try {
    await api('/api/run', { method: 'POST', body: JSON.stringify({ stores }) });
    showToast(stores ? 'Execução da loja iniciada.' : 'Execução das lojas iniciada.');
    await refreshStatus();
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderStoreManager() {
  const list = document.querySelector('#storeManagerList');
  const rows = latestStatus.stores.map(store => {
    const label = document.createElement('label');
    label.className = 'store-picker-row';
    const name = document.createElement('span');
    name.className = 'store-picker-name';
    name.textContent = store.name;
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = 'stores';
    input.value = store.key;
    input.checked = store.enabled;
    label.append(createStoreIcon(store), name, input);
    return label;
  });
  list.replaceChildren(...rows);
}

async function openStoreManager() {
  try {
    if (!latestStatus) await refreshStatus(false);
    renderStoreManager();
    document.querySelector('#storeModalBackdrop').hidden = false;
    document.querySelector('#storeManagerList input')?.focus();
  } catch (error) {
    showToast(error.message, true);
  }
}

function closeStoreManager() {
  document.querySelector('#storeModalBackdrop').hidden = true;
}

async function saveStoreSelection(event) {
  event.preventDefault();
  const stores = new FormData(event.currentTarget).getAll('stores');
  if (stores.length === 0) {
    showToast('Selecione pelo menos uma loja.', true);
    return;
  }
  try {
    await api('/api/config', {
      method: 'POST',
      body: JSON.stringify({ values: { STORES: stores } }),
    });
    closeStoreManager();
    showToast('Lojas atualizadas.');
    await refreshStatus();
  } catch (error) {
    showToast(error.message, true);
  }
}

function createInput(spec, current, configured) {
  const wrapper = document.createElement('div');
  wrapper.className = spec.kind === 'boolean' ? 'check-row' : 'field';
  const input = document.createElement('input');
  input.name = spec.key;
  if (spec.kind === 'boolean') {
    input.type = 'checkbox';
    input.checked = Boolean(current);
    const label = document.createElement('label');
    label.textContent = spec.label;
    wrapper.append(input, label);
    return wrapper;
  }
  input.type = spec.kind === 'integer' ? 'number' : (spec.secret ? 'password' : 'text');
  if (!spec.secret && current !== null && current !== undefined) input.value = current;
  if (spec.min !== null) input.min = spec.min;
  if (spec.max !== null) input.max = spec.max;
  input.placeholder = spec.secret && configured ? '•••••• configurado' : (spec.help || '');
  const label = document.createElement('label');
  label.textContent = spec.label;
  if (spec.secret && configured) {
    const marker = document.createElement('span');
    marker.className = 'configured';
    marker.textContent = '  ✓';
    label.append(marker);
  }
  wrapper.append(label, input);
  if (spec.help) {
    const help = document.createElement('small');
    help.textContent = spec.help;
    wrapper.append(help);
  }
  return wrapper;
}

function renderSettings(config) {
  latestConfig = config;
  const content = document.querySelector('#settingsContent');
  const fragments = [];
  const storeGroup = document.createElement('section');
  storeGroup.className = 'settings-group';
  const storeTitle = document.createElement('h3');
  storeTitle.textContent = 'Lojas habilitadas';
  const options = document.createElement('div');
  options.className = 'store-options';
  for (const store of latestStatus.stores) {
    const row = document.createElement('label');
    row.className = 'check-row';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = 'STORES';
    input.value = store.key;
    input.checked = config.values.STORES.includes(store.key);
    const text = document.createElement('span');
    text.textContent = store.name;
    row.append(input, text);
    options.append(row);
  }
  storeGroup.append(storeTitle, options);
  fragments.push(storeGroup);

  const sections = new Map();
  for (const spec of config.schema.filter(item => item.key !== 'STORES')) {
    if (!sections.has(spec.section)) sections.set(spec.section, []);
    sections.get(spec.section).push(spec);
  }
  for (const [name, specs] of sections) {
    const group = document.createElement('section');
    group.className = 'settings-group';
    const heading = document.createElement('h3');
    heading.textContent = name;
    const grid = document.createElement('div');
    grid.className = 'field-grid';
    for (const spec of specs) {
      grid.append(createInput(spec, config.values[spec.key], config.configured[spec.key]));
    }
    group.append(heading, grid);
    fragments.push(group);
  }
  content.replaceChildren(...fragments);
}

async function openSettings() {
  try {
    if (!latestStatus) await refreshStatus(false);
    renderSettings(await api('/api/config'));
    document.querySelector('#drawerBackdrop').hidden = false;
    document.querySelector('#settingsDrawer').classList.add('open');
    document.querySelector('#settingsDrawer').setAttribute('aria-hidden', 'false');
  } catch (error) {
    showToast(error.message, true);
  }
}

function closeSettings() {
  document.querySelector('#settingsDrawer').classList.remove('open');
  document.querySelector('#settingsDrawer').setAttribute('aria-hidden', 'true');
  setTimeout(() => { document.querySelector('#drawerBackdrop').hidden = true; }, 180);
}

async function saveSettings(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const values = { STORES: form.getAll('STORES') };
  for (const spec of latestConfig.schema.filter(item => item.key !== 'STORES')) {
    const input = event.currentTarget.elements[spec.key];
    values[spec.key] = spec.kind === 'boolean' ? input.checked : input.value;
  }
  try {
    const result = await api('/api/config', { method: 'POST', body: JSON.stringify({ values }) });
    showToast(`${result.changed.length} configuração(ões) salva(s).`);
    closeSettings();
    await refreshStatus();
  } catch (error) {
    showToast(error.message, true);
  }
}

document.querySelector('#vncLink').href = `${location.protocol}//${location.hostname}:7080/?autoconnect=true`;
document.querySelector('#runAllButton').addEventListener('click', () => runStores());
document.querySelector('#refreshButton').addEventListener('click', () => refreshStatus(false));
document.querySelector('#settingsButton').addEventListener('click', openSettings);
document.querySelector('#closeSettings').addEventListener('click', closeSettings);
document.querySelector('#drawerBackdrop').addEventListener('click', closeSettings);
document.querySelector('#settingsForm').addEventListener('submit', saveSettings);
document.querySelector('#closeStoreModal').addEventListener('click', closeStoreManager);
document.querySelector('#cancelStoreModal').addEventListener('click', closeStoreManager);
document.querySelector('#storeModalBackdrop').addEventListener('click', event => {
  if (event.target.id === 'storeModalBackdrop') closeStoreManager();
});
document.querySelector('#storeManagerForm').addEventListener('submit', saveStoreSelection);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    closeStoreManager();
    closeSettings();
  }
});

refreshStatus(false);
setInterval(() => refreshStatus(), 3000);
