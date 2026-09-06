(function exposeClaimerI18n(root) {
  function normalizeLocale(value) {
    const locale = String(value || '').toLowerCase();
    if (locale.startsWith('pt')) return 'pt-BR';
    if (locale.startsWith('es')) return 'es';
    return 'en';
  }

  function detectLocale(saved, languages = []) {
    const supported = new Set(['en', 'pt-BR', 'es']);
    if (supported.has(saved)) return saved;
    for (const language of languages) {
      const normalized = normalizeLocale(language);
      if (supported.has(normalized) && normalized !== 'en') return normalized;
      if (String(language || '').toLowerCase().startsWith('en')) return 'en';
    }
    return 'en';
  }

  const api = {normalizeLocale, detectLocale};
  root.ClaimerI18n = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
}(typeof window === 'undefined' ? globalThis : window));
