const token = document.querySelector('meta[name="fgc-token"]').content;
const storeGrid = document.querySelector('#storeGrid');
const toast = document.querySelector('#toast');
const supportedLocales = ['en', 'pt-BR', 'es'];
const {normalizeLocale, detectLocale} = window.ClaimerI18n;
const logoStores = new Set(['steam', 'epic', 'gog', 'ubisoft', 'aliexpress']);
const setupSteps = ['Language', 'Security', 'Stores', 'Accounts', 'Schedule', 'Review'];
let translations = {};
let currentLocale = 'en';
let latestStatus = null;
let latestConfig = null;
let activeSettingsSection = 'section.stores';
let setupStep = 0;
let setupValues = {};

function detectedLocale() {
  return detectLocale(
    localStorage.getItem('fgc-dashboard-language'),
    navigator.languages || [navigator.language],
  );
}

function valueAt(key) {
  return key.split('.').reduce((value, part) => value && value[part], translations);
}

function t(key, params = {}) {
  let text = valueAt(key);
  if (typeof text !== 'string') return key;
  for (const [name, value] of Object.entries(params)) {
    text = text.replaceAll(`{${name}}`, String(value));
  }
  return text;
}

async function setLocale(locale, persist = true) {
  currentLocale = normalizeLocale(locale);
  const response = await fetch(`/assets/locales/${currentLocale}.json`, { cache: 'no-store' });
  if (!response.ok) throw new Error('Unable to load language');
  translations = await response.json();
  document.documentElement.lang = currentLocale;
  document.title = t('app.title');
  document.querySelector('#languageSelect').value = currentLocale;
  if (persist) localStorage.setItem('fgc-dashboard-language', currentLocale);
  for (const element of document.querySelectorAll('[data-i18n]')) {
    element.textContent = t(element.dataset.i18n);
  }
  for (const element of document.querySelectorAll('[data-i18n-aria-label]')) {
    element.setAttribute('aria-label', t(element.dataset.i18nAriaLabel));
  }
  if (latestStatus) renderStatus(latestStatus);
  if (latestConfig && document.querySelector('#settingsDrawer').classList.contains('open')) renderSettings(latestConfig);
  if (!document.querySelector('#onboarding').hidden) renderOnboarding();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {'Content-Type': 'application/json', 'X-FGC-Token': token, ...(options.headers || {})},
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.code ? t(data.code) : (data.error || t('error.generic')));
    error.code = data.code;
    throw error;
  }
  return data;
}

function showToast(message, error = false) {
  toast.textContent = message;
  toast.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = 'toast'; }, 3500);
}

function relativeTime(value) {
  if (!value) return t('common.never');
  const date = new Date(value);
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(currentLocale, {numeric: 'auto'});
  for (const [size, unit] of [[86400, 'day'], [3600, 'hour'], [60, 'minute']]) {
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

const outcomeKeys = {
  claimed: 'status.claimed', owned: 'status.owned', available: 'status.available', failed: 'status.failedItem',
  skipped: 'status.skipped', action_required: 'status.actionRequired', processed: 'status.processed',
};

function createStoreDetails(details) {
  if (!details || !details.kind) return null;
  const panel = document.createElement('div');
  panel.className = 'store-results';
  if (details.kind === 'games') {
    const list = document.createElement('ul');
    list.className = 'result-list';
    for (const item of details.items || []) {
      const entry = document.createElement('li');
      const title = document.createElement('span');
      title.className = 'result-title';
      title.textContent = item.title;
      const outcome = document.createElement('span');
      outcome.className = `result-outcome outcome-${item.outcome}`;
      outcome.textContent = t(outcomeKeys[item.outcome] || 'status.processed');
      entry.append(title, outcome);
      list.append(entry);
    }
    panel.append(list);
    return panel;
  }
  if (details.kind === 'coins') {
    const coinKeys = {collected: 'coins.collected', collected_manual: 'coins.collectedManual', already_collected: 'coins.already', not_collected: 'coins.notCollected', available: 'coins.simulation'};
    const outcome = document.createElement('strong');
    outcome.className = `coin-outcome coin-${details.outcome}`;
    outcome.textContent = t(coinKeys[details.outcome] || 'coins.notCollected');
    const metrics = document.createElement('div');
    metrics.className = 'coin-metrics';
    const values = [];
    if (details.claimedCoins !== null && ['collected', 'collected_manual'].includes(details.outcome)) values.push(t('coins.gained', {count: details.claimedCoins}));
    else if (details.offeredCoins !== null) values.push(t('coins.offer', {count: details.offeredCoins}));
    if (details.balance !== null) values.push(t('coins.balance', {count: details.balance}));
    if (details.streakDays !== null) values.push(t(details.streakDays === 1 ? 'coins.streak' : 'coins.streakMany', {count: details.streakDays}));
    if (details.tomorrowCoins !== null) values.push(t('coins.tomorrow', {count: details.tomorrowCoins}));
    for (const value of values) {
      const metric = document.createElement('span');
      metric.textContent = value;
      metrics.append(metric);
    }
    panel.append(outcome, metrics);
    return panel;
  }
  return null;
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
  message.textContent = t(store.messageKey || 'status.waiting');
  state.append(marker, message);
  const lastRun = document.createElement('div');
  lastRun.className = 'last-run';
  const lastRunLabel = document.createElement('span');
  lastRunLabel.className = 'last-run-label';
  lastRunLabel.textContent = t('dashboard.lastExecution');
  const lastRunValue = document.createElement('strong');
  lastRunValue.className = 'last-run-value';
  lastRunValue.textContent = store.lastRun ? relativeTime(store.lastRun) : t('dashboard.sessionNever');
  lastRun.append(lastRunLabel, lastRunValue);
  const run = document.createElement('button');
  run.className = 'button run-store';
  run.type = 'button';
  run.textContent = t(store.state === 'running' ? 'status.runningButton' : 'status.run');
  run.disabled = globalRunning;
  run.addEventListener('click', () => runStores([store.key]));
  row.append(identity, state, lastRun, run);
  const details = createStoreDetails(store.details);
  if (details) row.append(details);
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
  title.textContent = t(availableCount ? 'store.add' : 'store.manage');
  const detail = document.createElement('small');
  detail.textContent = availableCount ? t(availableCount === 1 ? 'store.availableOne' : 'store.availableMany', {count: availableCount}) : t('store.allActive');
  copy.append(title, detail);
  button.append(symbol, copy);
  button.addEventListener('click', openStoreManager);
  return button;
}

function renderStatus(status) {
  latestStatus = status;
  const enabledStores = status.stores.filter(store => store.enabled);
  storeGrid.replaceChildren(...enabledStores.map(store => createStoreRow(store, status.running)), createAddStoreRow(status.stores.length - enabledStores.length));
  document.querySelector('#activeStoreCount').textContent = String(enabledStores.length);
  document.querySelector('#lastRun').textContent = relativeTime(status.finishedAt);
  document.querySelector('#nextRun').textContent = status.schedule.nextRun ? relativeTime(status.schedule.nextRun) : t('dashboard.manualOnly');
  document.querySelector('#runLabel').textContent = t(status.running ? 'dashboard.running' : 'dashboard.ready');
  document.querySelector('#runPill').classList.toggle('running', status.running);
  document.querySelector('#runAllButton').disabled = status.running || enabledStores.length === 0;
}

async function refreshStatus(silent = true) {
  try { renderStatus(await api('/api/status')); }
  catch (error) { if (!silent) showToast(error.message, true); }
}

async function runStores(stores = null) {
  try {
    await api('/api/run', {method: 'POST', body: JSON.stringify({stores})});
    showToast(t(stores ? 'store.runStarted' : 'store.allStarted'));
    await refreshStatus();
  } catch (error) { showToast(error.message, true); }
}

function renderStoreChoices(container, name, selected) {
  const rows = latestStatus.stores.map(store => {
    const label = document.createElement('label');
    label.className = 'store-picker-row';
    const copy = document.createElement('span');
    copy.className = 'store-picker-name';
    copy.textContent = store.name;
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = name;
    input.value = store.key;
    input.checked = selected.includes(store.key);
    label.append(createStoreIcon(store), copy, input);
    return label;
  });
  container.replaceChildren(...rows);
}

function renderStoreManager() {
  renderStoreChoices(document.querySelector('#storeManagerList'), 'stores', latestStatus.stores.filter(store => store.enabled).map(store => store.key));
}

async function openStoreManager() {
  try {
    if (!latestStatus) await refreshStatus(false);
    renderStoreManager();
    document.querySelector('#storeModalBackdrop').hidden = false;
    document.querySelector('#storeManagerList input')?.focus();
  } catch (error) { showToast(error.message, true); }
}

function closeStoreManager() { document.querySelector('#storeModalBackdrop').hidden = true; }

async function saveStoreSelection(event) {
  event.preventDefault();
  const stores = new FormData(event.currentTarget).getAll('stores');
  if (stores.length === 0) return showToast(t('store.selectOne'), true);
  try {
    await api('/api/config', {method: 'POST', body: JSON.stringify({values: {STORES: stores}})});
    closeStoreManager();
    showToast(t('store.updated'));
    await refreshStatus();
  } catch (error) { showToast(error.message, true); }
}

function createTooltip(key, id) {
  const group = document.createElement('span');
  group.className = 'help-group';
  const button = document.createElement('button');
  button.className = 'help-button';
  button.type = 'button';
  button.textContent = '?';
  button.setAttribute('aria-describedby', id);
  button.setAttribute('aria-expanded', 'false');
  const tooltip = document.createElement('span');
  tooltip.className = 'help-tooltip';
  tooltip.id = id;
  tooltip.role = 'tooltip';
  tooltip.textContent = t(key);
  button.addEventListener('click', () => {
    const open = group.classList.toggle('open');
    button.setAttribute('aria-expanded', String(open));
  });
  group.append(button, tooltip);
  return group;
}

function createInput(spec, current, configured, prefix = '') {
  const wrapper = document.createElement('div');
  wrapper.className = spec.kind === 'boolean' ? 'check-row' : 'field';
  const input = document.createElement('input');
  input.name = `${prefix}${spec.key}`;
  input.dataset.settingKey = spec.key;
  if (spec.kind === 'boolean') {
    input.type = 'checkbox';
    input.checked = Boolean(current);
    const label = document.createElement('label');
    label.textContent = t(spec.labelKey);
    wrapper.append(input, label);
    return wrapper;
  }
  input.type = spec.kind === 'integer' ? 'number' : (spec.secret ? 'password' : 'text');
  if (!spec.secret && current !== null && current !== undefined) input.value = current;
  if (spec.min !== null) input.min = spec.min;
  if (spec.max !== null) input.max = spec.max;
  input.placeholder = spec.secret && configured ? `•••••• ${t('common.configured')}` : (spec.helpKey ? t(spec.helpKey) : '');
  const label = document.createElement('label');
  const labelText = document.createElement('span');
  labelText.textContent = t(spec.labelKey);
  label.append(labelText);
  if (spec.credentialPurposeKey) label.append(createTooltip(spec.credentialPurposeKey, `help-${prefix}${spec.key}`));
  if (spec.secret && configured) {
    const marker = document.createElement('span');
    marker.className = 'configured';
    marker.textContent = '✓';
    label.append(marker);
  }
  wrapper.append(label, input);
  if (spec.helpKey) {
    const help = document.createElement('small');
    help.textContent = t(spec.helpKey);
    wrapper.append(help);
  }
  return wrapper;
}

function createSettingsPanel(sectionKey, body) {
  const panel = document.createElement('section');
  panel.className = 'settings-panel';
  panel.dataset.section = sectionKey;
  const header = document.createElement('header');
  header.className = 'settings-panel-header';
  const title = document.createElement('h3');
  title.textContent = t(sectionKey);
  const description = document.createElement('p');
  description.textContent = t(sectionKey.replace('section.', 'sectionDescription.'));
  header.append(title, description);
  panel.append(header, body);
  return panel;
}

function activateSettingsSection(name) {
  activeSettingsSection = name;
  for (const panel of document.querySelectorAll('.settings-panel')) panel.hidden = panel.dataset.section !== name;
  for (const button of document.querySelectorAll('.settings-nav-item')) {
    const active = button.dataset.section === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-current', active ? 'page' : 'false');
  }
}

function renderSettings(config) {
  latestConfig = config;
  const options = document.createElement('div');
  options.className = 'store-settings-list';
  renderStoreChoices(options, 'STORES', config.values.STORES);
  const panels = [createSettingsPanel('section.stores', options)];
  const sections = new Map();
  for (const spec of config.schema.filter(item => item.key !== 'STORES')) {
    if (!sections.has(spec.sectionKey)) sections.set(spec.sectionKey, []);
    sections.get(spec.sectionKey).push(spec);
  }
  for (const [sectionKey, specs] of sections) {
    const grid = document.createElement('div');
    grid.className = 'field-grid';
    for (const spec of specs) grid.append(createInput(spec, config.values[spec.key], config.configured[spec.key]));
    panels.push(createSettingsPanel(sectionKey, grid));
  }
  document.querySelector('#settingsContent').replaceChildren(...panels);
  const navItems = panels.map(panel => {
    const button = document.createElement('button');
    button.className = 'settings-nav-item';
    button.type = 'button';
    button.dataset.section = panel.dataset.section;
    button.textContent = t(panel.dataset.section);
    button.addEventListener('click', () => activateSettingsSection(panel.dataset.section));
    return button;
  });
  document.querySelector('#settingsNav').replaceChildren(...navItems);
  if (!panels.some(panel => panel.dataset.section === activeSettingsSection)) activeSettingsSection = 'section.stores';
  activateSettingsSection(activeSettingsSection);
}

async function openSettings() {
  try {
    if (!latestStatus) await refreshStatus(false);
    renderSettings(await api('/api/config'));
    document.querySelector('#drawerBackdrop').hidden = false;
    document.querySelector('#settingsDrawer').classList.add('open');
    document.querySelector('#settingsDrawer').setAttribute('aria-hidden', 'false');
  } catch (error) { showToast(error.message, true); }
}

function closeSettings() {
  document.querySelector('#settingsDrawer').classList.remove('open');
  document.querySelector('#settingsDrawer').setAttribute('aria-hidden', 'true');
  setTimeout(() => { document.querySelector('#drawerBackdrop').hidden = true; }, 180);
}

function valuesFromForm(form, schema, prefix = '') {
  const formData = new FormData(form);
  const values = {};
  for (const spec of schema) {
    if (spec.kind === 'stores') values[spec.key] = formData.getAll(`${prefix}${spec.key}`);
    else {
      const input = form.querySelector(`[data-setting-key="${spec.key}"]`);
      if (input) values[spec.key] = spec.kind === 'boolean' ? input.checked : input.value;
    }
  }
  return values;
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    const values = valuesFromForm(event.currentTarget, latestConfig.schema);
    const result = await api('/api/config', {method: 'POST', body: JSON.stringify({values})});
    showToast(t('settings.saved', {count: result.changed.length}));
    closeSettings();
    await refreshStatus();
  } catch (error) { showToast(error.message, true); }
}

function setupSelectedStores() { return Array.isArray(setupValues.STORES) ? setupValues.STORES : []; }

function captureSetupStep() {
  const form = document.querySelector('#onboardingForm');
  for (const input of form.querySelectorAll('[data-setting-key]')) {
    if (input.type === 'checkbox' && input.dataset.settingKey === 'STORES') continue;
    setupValues[input.dataset.settingKey] = input.type === 'checkbox' ? input.checked : input.value;
  }
  const stores = [...form.querySelectorAll('input[data-setting-key="STORES"]:checked')].map(input => input.value);
  if (form.querySelector('input[data-setting-key="STORES"]')) setupValues.STORES = stores;
}

function setupHeader(titleKey, copyKey) {
  const header = document.createElement('header');
  header.className = 'setup-page-header';
  const title = document.createElement('h2');
  title.textContent = t(titleKey);
  const copy = document.createElement('p');
  copy.textContent = t(copyKey);
  header.append(title, copy);
  return header;
}

function setupLanguagePage() {
  const page = document.createElement('section');
  page.append(setupHeader('onboarding.languageTitle', 'onboarding.languageCopy'));
  const choices = document.createElement('div');
  choices.className = 'language-cards';
  for (const [locale, label] of [['en', 'English'], ['pt-BR', 'Português do Brasil'], ['es', 'Español']]) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `language-card${currentLocale === locale ? ' selected' : ''}`;
    button.textContent = label;
    button.addEventListener('click', () => setLocale(locale));
    choices.append(button);
  }
  page.append(choices);
  return page;
}

function setupSecurityPage() {
  const page = document.createElement('section');
  page.append(setupHeader('onboarding.securityTitle', 'onboarding.securityCopy'));
  const card = document.createElement('div');
  card.className = 'security-detail-card';
  for (const key of ['security.title', 'security.summary', 'security.honest', 'security.notEncrypted', 'security.scope']) {
    const paragraph = document.createElement(key === 'security.title' ? 'h3' : 'p');
    paragraph.textContent = t(key);
    card.append(paragraph);
  }
  page.append(card);
  return page;
}

function setupStoresPage() {
  const page = document.createElement('section');
  page.append(setupHeader('onboarding.storesTitle', 'onboarding.storesCopy'));
  const choices = document.createElement('div');
  choices.className = 'store-picker setup-store-picker';
  renderStoreChoices(choices, 'setup-STORES', setupSelectedStores());
  for (const input of choices.querySelectorAll('input')) input.dataset.settingKey = 'STORES';
  page.append(choices);
  return page;
}

function credentialSectionKeys() {
  const stores = new Set(setupSelectedStores());
  const sections = new Set([...stores].map(store => `section.${store}`));
  if (stores.has('fab')) sections.add('section.epic');
  if (stores.has('gamerpower')) {
    sections.add('section.fanatical');
    sections.add('section.itchio');
    sections.add('section.indiegala');
  }
  return sections;
}

function setupAccountsPage() {
  const page = document.createElement('section');
  page.append(setupHeader('onboarding.accountsTitle', 'onboarding.accountsCopy'));
  const sections = credentialSectionKeys();
  const specs = latestConfig.schema.filter(spec => spec.credentialPurposeKey && sections.has(spec.sectionKey));
  const grid = document.createElement('div');
  grid.className = 'field-grid setup-fields';
  for (const spec of specs) {
    const field = createInput(spec, '', latestConfig.configured[spec.key], 'setup-');
    const input = field.querySelector('input');
    if (input && setupValues[spec.key]) input.value = setupValues[spec.key];
    grid.append(field);
  }
  page.append(grid);
  return page;
}

function setupSchedulePage() {
  const page = document.createElement('section');
  page.append(setupHeader('onboarding.scheduleTitle', 'onboarding.scheduleCopy'));
  const wanted = new Set(['SCHEDULER_HOURS', 'SCHEDULER_FIXED_TIMES', 'SCHEDULER_TIMEZONE', 'RUN_ON_STARTUP']);
  const grid = document.createElement('div');
  grid.className = 'field-grid setup-fields';
  for (const spec of latestConfig.schema.filter(item => wanted.has(item.key))) {
    grid.append(createInput(spec, setupValues[spec.key], latestConfig.configured[spec.key], 'setup-'));
  }
  page.append(grid);
  return page;
}

function setupReviewPage() {
  const page = document.createElement('section');
  page.append(setupHeader('onboarding.reviewTitle', 'onboarding.reviewCopy'));
  const review = document.createElement('dl');
  review.className = 'setup-review';
  const selected = setupSelectedStores();
  const entered = Object.entries(setupValues).filter(([key, value]) => latestConfig.schema.some(spec => spec.key === key && spec.secret) && value).length;
  const rows = [
    [t('onboarding.selectedStores'), selected.map(key => latestStatus.stores.find(store => store.key === key)?.name || key).join(', ')],
    [t('onboarding.credentialsConfigured'), String(entered)],
    [t('onboarding.interval'), Number(setupValues.SCHEDULER_HOURS) > 0 ? t('onboarding.hours', {count: setupValues.SCHEDULER_HOURS}) : t('onboarding.manualSchedule')],
    [t('security.title'), t('onboarding.localOnly')],
  ];
  for (const [term, description] of rows) {
    const group = document.createElement('div');
    const dt = document.createElement('dt');
    dt.textContent = term;
    const dd = document.createElement('dd');
    dd.textContent = description;
    group.append(dt, dd);
    review.append(group);
  }
  page.append(review);
  return page;
}

function renderOnboarding() {
  const steps = setupSteps.map((name, index) => {
    const item = document.createElement('li');
    item.className = index === setupStep ? 'active' : (index < setupStep ? 'complete' : '');
    const number = document.createElement('span');
    number.textContent = index < setupStep ? '✓' : String(index + 1);
    const label = document.createElement('span');
    label.textContent = t(`onboarding.step${name}`);
    item.append(number, label);
    return item;
  });
  document.querySelector('#onboardingSteps').replaceChildren(...steps);
  const factories = [setupLanguagePage, setupSecurityPage, setupStoresPage, setupAccountsPage, setupSchedulePage, setupReviewPage];
  const content = document.querySelector('#onboardingContent');
  content.replaceChildren(factories[setupStep]());
  content.scrollTo({top: 0, left: 0});
  document.querySelector('#setupBack').hidden = setupStep === 0;
  document.querySelector('#setupNext').textContent = t(setupStep === setupSteps.length - 1 ? 'onboarding.finish' : 'common.continue');
}

async function nextSetupStep() {
  captureSetupStep();
  if (setupStep === 2 && setupSelectedStores().length === 0) return showToast(t('store.selectOne'), true);
  if (setupStep < setupSteps.length - 1) {
    setupStep += 1;
    renderOnboarding();
    return;
  }
  try {
    await api('/api/setup', {method: 'POST', body: JSON.stringify({values: setupValues})});
    document.querySelector('#onboarding').hidden = true;
    await refreshStatus(false);
    await runStores();
  } catch (error) { showToast(error.message, true); }
}

function previousSetupStep() {
  captureSetupStep();
  setupStep = Math.max(0, setupStep - 1);
  renderOnboarding();
}

async function initialise() {
  await setLocale(detectedLocale(), false);
  latestStatus = await api('/api/status');
  latestConfig = await api('/api/config');
  setupValues = {...latestConfig.values, STORES: [...latestConfig.values.STORES]};
  renderStatus(latestStatus);
  if (latestConfig.setup.required && !latestConfig.setup.complete) {
    document.querySelector('#onboarding').hidden = false;
    renderOnboarding();
  }
}

document.querySelector('#vncLink').href = `${location.protocol}//${location.hostname}:7080/?autoconnect=true`;
document.querySelector('#languageSelect').addEventListener('change', event => setLocale(event.target.value));
document.querySelector('#runAllButton').addEventListener('click', () => runStores());
document.querySelector('#refreshButton').addEventListener('click', () => refreshStatus(false));
document.querySelector('#settingsButton').addEventListener('click', openSettings);
document.querySelector('#closeSettings').addEventListener('click', closeSettings);
document.querySelector('#drawerBackdrop').addEventListener('click', closeSettings);
document.querySelector('#settingsForm').addEventListener('submit', saveSettings);
document.querySelector('#closeStoreModal').addEventListener('click', closeStoreManager);
document.querySelector('#cancelStoreModal').addEventListener('click', closeStoreManager);
document.querySelector('#storeModalBackdrop').addEventListener('click', event => { if (event.target.id === 'storeModalBackdrop') closeStoreManager(); });
document.querySelector('#storeManagerForm').addEventListener('submit', saveStoreSelection);
document.querySelector('#setupBack').addEventListener('click', previousSetupStep);
document.querySelector('#setupNext').addEventListener('click', nextSetupStep);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && document.querySelector('#onboarding').hidden) { closeStoreManager(); closeSettings(); }
});
document.addEventListener('click', event => {
  if (!event.target.closest('.help-group')) {
    for (const group of document.querySelectorAll('.help-group.open')) group.classList.remove('open');
  }
});

initialise().catch(error => showToast(error.message || t('error.generic'), true));
setInterval(() => refreshStatus(), 3000);
