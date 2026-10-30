// ==UserScript==
// @name         Postmuxarr → disponibilité sur HD-Forever
// @namespace    postmuxarr
// @version      3.5
// @description  Sur les autres trackers (PTP, ABN, TR4KER, Nostradamus, C411), indique si le film est sur HD-Forever, dans quels formats, et si chaque release de la page (Blu-ray Original, remux, rip, mHD, WEB-DL) y serait un doublon. Fiche : automatique ; liste : bouton par film.
// @match        https://passthepopcorn.me/torrents.php*
// @match        https://abn.lol/Torrent*
// @match        https://abn.lol/Group/Details*
// @include      *://*tr4ker*/*
// @include      *://*nostradamus*/*
// @match        https://c411.org/*
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @connect      __POSTMUXARR_HOST__
// @updateURL    __SELF_URL__
// @downloadURL  __SELF_URL__
// @inject-into  page
// @run-at       document-idle
// ==/UserScript==

(() => {
  'use strict';
  // Adresse et clé remplies par Postmuxarr au téléchargement (Réglages ▸ Userscripts) : réinstaller si elles changent
  const POSTMUXARR = '__POSTMUXARR_URL__';
  const API_KEY = '__API_KEY__';                     // vide si la protection par mot de passe est désactivée
  const AUTO_DETAIL = true;                        // fiche : vérification dès l'ouverture (1 appel HDF, puis cache)
  const W = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
  // Compatibilité : API GM_* (Violentmonkey, Tampermonkey) ou GM.* (Greasemonkey 4, Userscripts pour Safari)
  const GMX = typeof GM_xmlhttpRequest === 'function' ? GM_xmlhttpRequest
    : typeof GM !== 'undefined' && GM && typeof GM.xmlHttpRequest === 'function' ? o => GM.xmlHttpRequest(o) : null;
  const xhr = (reject, o) => GMX ? GMX(o)
    : reject(new Error('Ce gestionnaire de userscripts ne fournit pas GM_xmlhttpRequest : utilise Violentmonkey ou Tampermonkey'));

  const VERDICT = {
    absent: ['Repostable sur HDF', '#67c99a'],
    allowed: ['Doublon toléré (HDForever)', '#f3b443'],
    dupe: ['Doublon sur HDF', '#ec6b6b'],
    identical: ['Identique sur HDF', '#ec6b6b'],
    reseed: ['Identique sans seeder sur HDF : à reseeder', '#67c99a'],
    replaceable: ['Doublon sans seeder : voir la note modo HDF', '#f3b443'],
    review: ['Même catégorie sur HDF : à arbitrer', '#f3b443'],
    refused: ['WEBRip : refusé sur HDF', '#ec6b6b'],
    ambiguous: ['Plusieurs films trouvés', '#f3b443'],
    missing: ['Film absent de HDF', '#67c99a'],
    present: ['Film sur HDF : ouvrir pour comparer', '#8d95b8'],
  };
  // PTP : le libellé du torrent dit la catégorie mieux que le nom (« BD50 / m2ts », « … / Remux »)
  const ptpKind = label => /\bRemux\b/i.test(label) ? 'remux'
    : /\b(BD25|BD50|BD66|BD100|m2ts)\b/i.test(label) || (/\bISO\b/i.test(label) && /Blu-ray/i.test(label)) ? 'original' : '';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const go = b => b ? (b / 1e9).toFixed(1).replace('.', ',') + ' Go' : '?';
  // « 11.76 GiB », « 25,5 Go » → octets (arrondi). Les trackers français affichent « Go » pour des Gio.
  const UNITS = {KIB: 2 ** 10, MIB: 2 ** 20, GIB: 2 ** 30, TIB: 2 ** 40, KB: 1e3, MB: 1e6, GB: 1e9, TB: 1e12,
    KO: 2 ** 10, MO: 2 ** 20, GO: 2 ** 30, TO: 2 ** 40, KIO: 2 ** 10, MIO: 2 ** 20, GIO: 2 ** 30, TIO: 2 ** 40};
  function parseSize(s) {
    const m = String(s || '').replace(/\s(?=\d)/g, '').match(/([\d.,]+)\s*([KMGT]i?B|[KMGT]i?o)\b/i);
    if (!m) return 0;
    const n = parseFloat(m[1].replace(/,(?=\d{3}\b)/g, '').replace(',', '.'));
    return Math.round(n * (UNITS[m[2].toUpperCase()] || 1));
  }

  function lookup(body) {
    return new Promise((resolve, reject) => xhr(reject, {
      method: 'POST', url: POSTMUXARR + '/api/hdf/lookup', timeout: 180000,
      headers: {'Content-Type': 'application/json', ...(API_KEY ? {'X-Api-Key': API_KEY} : {})}, data: JSON.stringify(body),
      onload: r => { let j = null; try { j = JSON.parse(r.responseText); } catch (e) {}
        r.status === 401 ? reject(new Error('Postmuxarr refuse l\'accès : réinstalle le userscript depuis Réglages ▸ Userscripts'))
          : r.status < 400 ? resolve(j) : reject(new Error((j && j.detail) || `Postmuxarr : HTTP ${r.status}`)); },
      onerror: () => reject(new Error(`Postmuxarr injoignable (${POSTMUXARR})`)),
      ontimeout: () => reject(new Error('Postmuxarr : délai dépassé')),
    }));
  }

  const css = document.createElement('style');
  css.textContent = `
    .pmx-box{background:#171e36;color:#e7e9f3;border:1px solid #2b3558;border-radius:8px;padding:8px 10px;margin:6px 0;
      font:12px/1.45 system-ui,sans-serif;text-align:left}
    .pmx-box b.pmx-h{color:#f3b443}
    .pmx-box a{color:#f3b443}
    .pmx-box .pmx-m{color:#8d95b8}
    .pmx-box table{border-collapse:collapse;margin-top:4px;width:100%}
    .pmx-box td{padding:1px 8px 1px 0;border:0;background:none;color:inherit;white-space:nowrap}
    .pmx-box td.pmx-n{white-space:normal;word-break:break-all;color:#8d95b8;font:11px ui-monospace,monospace}
    .pmx-btn{margin-left:6px;padding:0 6px;font:11px system-ui,sans-serif;background:#1f2846;color:#e7e9f3;
      border:1px solid #2b3558;border-radius:5px;cursor:pointer;vertical-align:middle}
    .pmx-btn:disabled{opacity:.6;cursor:default}
    .pmx-tag{display:inline-block;margin-left:6px;padding:0 5px;border:1px solid currentColor;border-radius:4px;
      font:11px system-ui,sans-serif;vertical-align:middle}`;
  document.head.appendChild(css);

  const tag = st => VERDICT[st] ? `<span class="pmx-tag" style="color:${VERDICT[st][1]}">${VERDICT[st][0]}</span>` : '';

  // Tableau des formats présents sur HDF + verdict de chaque remux de la page
  function render(res, compact) {
    const t = res.torrents;
    let html = `<b class="pmx-h">HD-Forever</b> `;
    if (res.ambiguous) html += `<span style="color:#f3b443">plusieurs films correspondent : ${esc(res.films.join(', '))}</span>`;
    else if (!t.length) html += `<span style="color:#67c99a">absent de HDF</span>`;
    else html += `<span class="pmx-m">${t.length} torrent(s)</span> · <a href="${esc(t[0].url)}" target="_blank" rel="noopener">fiche</a>`;
    if (t.length && !res.ambiguous) html += `<table>${t.slice(0, compact ? 6 : 50).map(x => `<tr>
      <td><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.media || '?')}</a></td><td>${esc(x.encoding)}</td>
      <td>${esc(x.format)}</td><td>${go(x.size)}</td><td${x.seeders === 0 ? ' style="color:#f3b443"' : ''}>${x.seeders ?? '?'} S</td>
      ${compact ? '' : `<td class="pmx-n">${esc(x.name)}</td>`}</tr>`).join('')}</table>
      ${compact && t.length > 6 ? `<span class="pmx-m">+ ${t.length - 6} autre(s)</span>` : ''}`;
    const rel = res.releases.filter(r => VERDICT[r.status]);
    if (compact && rel.length) html += `<div style="margin-top:4px">${rel.map(r =>
      `<div>${tag(r.status)} <span class="pmx-m" style="font:11px ui-monospace,monospace">${esc(r.name)}</span></div>`).join('')}</div>`;
    const at = new Date(res.at * 1000).toLocaleString('fr-FR', {dateStyle: 'short', timeStyle: 'short'});
    html += `<div class="pmx-m" style="margin-top:3px">vérifié le ${at}${compact ? '' : ' · <a href="#" class="pmx-re">revérifier</a>'}</div>`;
    return html;
  }

  /* ------------------------------------------------------------ listes : un bouton par ligne, et vérification de masse */
  // Verdict d'une ligne (un film peut avoir plusieurs releases) : la plus favorable l'emporte
  const RANK = ['absent', 'missing', 'allowed', 'review', 'present', 'replaceable', 'reseed', 'identical', 'dupe', 'refused', 'ambiguous'];
  const OK = new Set(['absent', 'allowed', 'review', 'missing', 'present']);  // repostable, éventuellement après arbitrage
  function rowStatus(res) {
    if (res.ambiguous) return 'ambiguous';
    if (!res.releases.length) return res.torrents.length ? 'present' : 'missing';  // film sans release connue (C411)
    const st = res.releases.map(r => r.status).filter(x => RANK.includes(x)).sort((a, b) => RANK.indexOf(a) - RANK.indexOf(b));
    return st[0] || 'na';
  }
  const ROWS = [];  // {b, run} de toutes les lignes de la page, dans l'ordre
  // query() : corps de la recherche ; attach(box) : place le cadre de détail ; row : élément masquable de la ligne
  function rowButton(query, attach, row) {
    const b = document.createElement('button');
    b.className = 'pmx-btn'; b.type = 'button'; b.textContent = 'HDF ?';
    b.title = 'Vérifier la disponibilité sur HD-Forever';
    let box = null;
    const item = {b, row, status: null};
    item.run = async quiet => {  // quiet (vérification de masse) : étiquette seule, cadre au clic
      b.disabled = true; b.textContent = 'HDF…';
      try {
        item.res = await lookup(query());
        item.status = rowStatus(item.res);
        const v = VERDICT[item.status];
        b.textContent = v ? v[0] : item.res.torrents.length ? `HDF ✓ ${item.res.torrents.length}` : 'HDF ✗';
        b.style.color = v ? v[1] : ''; b.style.borderColor = v ? v[1] : '';
        b.title = `${item.res.torrents.length} torrent(s) sur HDF · clic : détail`;
        if (!quiet) { box = document.createElement('div'); box.className = 'pmx-box'; box.innerHTML = render(item.res, true); attach(box); }
      } catch (err) {
        item.status = 'error'; b.textContent = 'HDF ⚠'; b.title = err.message;
        if (!quiet) { box = document.createElement('div'); box.className = 'pmx-box'; box.innerHTML = `<span style="color:#ec6b6b">${esc(err.message)}</span>`; attach(box); }
      }
      b.disabled = false;
      bulkUpdate();
    };
    b.onclick = e => {
      e.preventDefault(); e.stopPropagation();  // le bouton peut être dans un lien
      if (box) { box.remove(); box = null; return; }
      if (item.res) { box = document.createElement('div'); box.className = 'pmx-box'; box.innerHTML = render(item.res, true); attach(box); return; }
      item.run(false);
    };
    ROWS.push(item);
    clearTimeout(bulkUpdate.t); bulkUpdate.t = setTimeout(bulkUpdate, 0);  // une fois le bouton inséré dans la page
    return b;
  }

  // Panneau flottant : vérifie toutes les lignes une par une (l'API HDF : ~10 s par film, cache 6 h côté Postmuxarr)
  let panel = null, running = false, stop = false, hideBad = false;
  function bulkUpdate() {
    for (let i = ROWS.length - 1; i >= 0; i--) if (!document.contains(ROWS[i].b)) ROWS.splice(i, 1);  // page changée (TR4KER)
    if (panel) panel.hidden = !ROWS.length;
    if (!ROWS.length) return;
    if (!panel) {
      panel = document.createElement('div');
      panel.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:99999;background:#171e36;color:#e7e9f3;'
        + 'border:1px solid #f3b443;border-radius:8px;padding:8px 10px;font:12px/1.5 system-ui,sans-serif;max-width:320px';
      panel.addEventListener('click', e => {
        const a = e.target.dataset.pmxa; if (!a) return;
        if (a !== 'hide') e.preventDefault();  // la case doit pouvoir se cocher
        if (a === 'go') bulkRun(); else if (a === 'stop') stop = true;
        else if (a === 'hide') { hideBad = e.target.checked; applyHide(); }
        else if (a === 'x') panel.style.display = 'none';
      });
      document.body.appendChild(panel);
    }
    const done = ROWS.filter(r => r.status), todo = ROWS.length - done.length;
    const count = {}; done.forEach(r => { count[r.status] = (count[r.status] || 0) + 1; });
    const sum = Object.entries(count).sort((a, b) => RANK.indexOf(a[0]) - RANK.indexOf(b[0]))
      .map(([k, n]) => `<span style="color:${(VERDICT[k] || ['', '#8d95b8'])[1]}">${(VERDICT[k] || [k === 'na' ? 'Hors catégorie' : 'Erreur'])[0]} : ${n}</span>`).join('<br>');
    panel.innerHTML = `<div style="display:flex;justify-content:space-between;gap:8px"><b style="color:#f3b443">HD-Forever</b>
        <a href="#" data-pmxa="x" style="color:#8d95b8;text-decoration:none">✕</a></div>
      ${running ? `${done.length}/${ROWS.length} · ~${Math.ceil(todo * 10.5 / 60)} min restante(s) <button class="pmx-btn" data-pmxa="stop">Arrêter</button>`
        : todo ? `<button class="pmx-btn" style="margin:2px 0" data-pmxa="go">Vérifier les ${todo} ligne(s)</button> <span style="color:#8d95b8">~${Math.ceil(todo * 10.5 / 60)} min</span>`
        : `${ROWS.length} ligne(s) vérifiée(s)`}
      ${sum ? `<div style="margin-top:4px">${sum}</div>` : ''}
      ${done.length ? `<label style="display:block;margin-top:4px;color:#8d95b8"><input type="checkbox" data-pmxa="hide" ${hideBad ? 'checked' : ''}> masquer ce qui n'est pas repostable</label>` : ''}`;
  }
  function applyHide() {
    ROWS.forEach(r => { if (r.row) r.row.style.display = hideBad && r.status && !OK.has(r.status) ? 'none' : ''; });
  }
  async function bulkRun() {
    running = true; stop = false; bulkUpdate();
    for (const r of ROWS) {  // l'une après l'autre : Postmuxarr ne peut pas aller plus vite que l'API HDF
      if (stop) break;
      if (r.status || !document.contains(r.b)) continue;
      await r.run(true);
      applyHide();
    }
    running = false; bulkUpdate();
  }

  /* ------------------------------------------------------------ fiche : commun à tous les sites */
  // where : élément avant lequel placer le panneau ; query : {imdb, tmdb, title, year} ;
  // rows : [{badge (élément après lequel placer le verdict), name, size, approx, remux}]
  function detailPanel(where, query, rows) {
    const box = document.createElement('div');
    box.className = 'pmx-box';
    where.parentNode.insertBefore(box, where);
    async function run(refresh) {
      box.innerHTML = '<b class="pmx-h">HD-Forever</b> <span class="pmx-m">vérification…</span>';
      document.querySelectorAll('.pmx-tag').forEach(e => e.remove());
      try {
        const res = await lookup({...query, refresh,
          releases: rows.map(r => ({name: r.name, size: r.size, approx: !!r.approx, remux: !!r.remux, kind: r.kind || ''}))});
        box.innerHTML = render(res, false);
        res.releases.forEach((v, i) => { if (VERDICT[v.status]) rows[i].badge.insertAdjacentHTML('afterend', tag(v.status)); });
      } catch (e) { box.innerHTML = `<b class="pmx-h">HD-Forever</b> <span style="color:#ec6b6b">${esc(e.message)}</span>`; }
    }
    box.addEventListener('click', e => { if (e.target.classList.contains('pmx-re')) { e.preventDefault(); run(true); } });
    if (AUTO_DETAIL) run(false);
    else { box.innerHTML = '<button class="pmx-btn">Vérifier la disponibilité sur HDF</button>'; box.firstChild.onclick = () => run(false); }
    return true;
  }

  /* ------------------------------------------------------------ PTP : fiche film */
  function ptpDetail() {
    const table = document.getElementById('torrent-table');
    if (!table) return false;
    const imdb = ((document.getElementById('imdb-title-link') || {}).href || '').match(/tt(\d+)/);
    const title = (document.querySelector('h2.page__title') || {}).textContent || '';
    const tm = title.match(/^(.*?)\s*\[(\d{4})\]/);
    const rows = [...document.querySelectorAll('tr.group_torrent_header[data-releasename]')].map(tr => {
      const sz = tr.querySelector('span[title$="bytes"]');  // taille à l'octet
      const label = (tr.querySelector('a.torrent-info-link') || {}).textContent || '';
      return {badge: tr.querySelector('a.torrent-info-link') || tr.cells[0], name: tr.dataset.releasename,
        remux: /\bRemux\b/i.test(label), kind: ptpKind(label),
        size: sz ? parseInt(sz.title.replace(/\D/g, ''), 10) : parseSize((tr.querySelector('td.nobr') || {}).textContent),
        approx: !sz};
    });
    return detailPanel(table, {imdb: imdb ? imdb[1] : '', title: tm ? tm[1] : title, year: tm ? tm[2] : ''}, rows);
  }

  /* ------------------------------------------------------------ ABN : fiche d'un torrent ou d'un groupe */
  // Pas d'IMDb/TMDB sur la page : recherche HDF par titre original + année de diffusion.
  // La table « Torrents dans le groupe » liste toutes les releases du film (tailles arrondies, « Go » = Gio).
  function abnDetail() {
    const grid = document.querySelector('[id^="Group_"][id$="_Grid"]');
    if (!grid) return false;
    const field = label => {
      const l = [...document.querySelectorAll('span.text-underline')].find(e => e.textContent.trim() === label);
      return l && l.nextElementSibling ? l.nextElementSibling.textContent.replace(/^\s*:\s*/, '').trim() : '';
    };
    const title = field('Titre original') || ((document.querySelector('.item-title') || {}).textContent || '').trim()
      || ((document.querySelector('h3 a[href*="/Group/Details"]') || {}).textContent || '').trim();
    const year = (field('Diffusion').match(/(\d{4})\s*$/) || [])[1]
      || ((document.querySelector('h1') || {}).textContent.match(/\b(19|20)\d{2}\b/) || [''])[0];
    const rows = [...grid.querySelectorAll('a[id^="torrent_link_"]')].map(a => {
      const cells = a.closest('tr').cells;
      const name = a.textContent.trim();
      return {badge: a, name, remux: /\bREMUX\b/i.test(name), approx: true,
        size: parseSize([...cells].map(c => c.textContent).find(t => /\d\s*[KMGT]o\b/i.test(t)))};
    });
    const where = [...document.querySelectorAll('h3')].find(h => /Torrents dans le groupe/i.test(h.textContent)) || grid;
    return detailPanel(where, {title, year}, rows);
  }

  /* ------------------------------------------------------------ liste (browse) */
  function pageData() {
    if (W.PageData && W.PageData.Movies) return W.PageData;
    for (const s of document.scripts) {  // repli : relire le JSON dans le code de la page
      const m = s.textContent.match(/var PageData = (\{[\s\S]*?\});\s*\n/);
      if (m) try { return JSON.parse(m[1]); } catch (e) {}
    }
    return null;
  }
  function ptpList() {
    const view = document.getElementById('torrents-movie-view');
    const data = view && pageData();
    if (!data || !data.Movies) return false;
    const movies = data.Movies;
    const releasesOf = m => (m.GroupingQualities || []).flatMap(g => g.Torrents || [])
      .map(t => ({name: t.ReleaseName || '', size: parseSize(t.Size), approx: true,
        kind: ptpKind(String(t.Title || '').replace(/<[^>]+>/g, ''))})).filter(r => r.name);
    function anchorFor(gid) {  // lien du titre du film (pas un lien de torrent)
      const links = [...view.querySelectorAll(`a[href*="torrents.php?id=${gid}"]`)]
        .filter(a => !/torrentid|action=/.test(a.getAttribute('href')));
      return links.find(a => a.textContent.trim()) || links[0];
    }
    function place() {
      movies.forEach(m => {
        const a = anchorFor(m.GroupId);
        if (!a || a.dataset.pmx) return;
        a.dataset.pmx = '1';
        const cell = a.closest('td, .cover-movie-list__movie, div') || a.parentNode;
        const b = rowButton(() => ({imdb: m.ImdbId || '', title: m.Title.split(' AKA ')[0], year: m.Year, releases: releasesOf(m)}),
          box => cell.appendChild(box), a.closest('tr, .cover-movie-list__movie') || cell);
        a.insertAdjacentElement('afterend', b);
      });
    }
    place();
    new MutationObserver(place).observe(view, {childList: true, subtree: true});  // la liste est dessinée par browse.js
    return true;
  }

  /* ------------------------------------------------------------ ABN : liste des torrents */
  // Une ligne par release, sans fiche film : titre et année tirés du nom de release, un bouton par ligne.
  function titleYear(name) {
    const m = name.match(/^(.*?)[.\s_(\[-]+((?:19|20)\d{2})(?=[.\s_)\]-]|$)/);
    return m ? {title: m[1].replace(/[._]+/g, ' ').trim(), year: m[2]} : {title: name.replace(/[._]+/g, ' '), year: ''};
  }
  function abnList() {
    const links = [...document.querySelectorAll('.mvc-grid a[id^="torrent_link_"]')];
    if (!links.length) return false;
    links.forEach(a => {
      if (a.dataset.pmx) return;
      a.dataset.pmx = '1';
      const tr = a.closest('tr'), name = a.textContent.trim();
      const size = parseSize([...tr.cells].map(c => c.textContent).find(t => /\d\s*[KMGT]o\b/i.test(t)));
      const b = rowButton(() => ({...titleYear(name), releases: [{name, size, approx: true, remux: /\bREMUX\b/i.test(name)}]}),
        box => a.closest('td').appendChild(box), tr);
      (a.parentNode.querySelector('img.torrent-icons:last-of-type') || a).insertAdjacentElement('afterend', b);
    });
    return true;
  }

  /* ------------------------------------------------------------ TR4KER (application monopage) */
  // Classes CSS générées (« _rowNameText_1usm0_135 ») : on ne garde que leur préfixe stable.
  // Le nom peut finir par le titre français entre parenthèses : retiré du nom, gardé comme 2e titre de recherche.
  function splitAlt(full) {
    const alt = (full.match(/\s\(([^()]+)\)$/) || [])[1] || '';
    return {name: alt ? full.slice(0, -alt.length - 3).trim() : full, alt};
  }

  // Liste : un bouton par ligne (la ligne entière est un lien vers la fiche)
  function tr4kerRows() {
    document.querySelectorAll('a[href^="/torrent/"]').forEach(a => {
      const nameEl = a.querySelector('[class*="_rowNameText_"]');
      if (!nameEl || a.dataset.pmx) return;
      a.dataset.pmx = '1';
      const {name, alt} = splitAlt(nameEl.textContent.trim());
      let cell = a.querySelector('[class*="_cellSize_"]');  // repli si le navigateur a coupé le lien (lien dans un lien)
      for (let n = a.nextElementSibling; !cell && n && !n.querySelector('[class*="_rowNameText_"]'); n = n.nextElementSibling)
        cell = n.matches('[class*="_cellSize_"]') ? n : n.querySelector('[class*="_cellSize_"]');
      const size = parseSize((cell || {}).textContent);
      // cadre placé hors du lien de la ligne, sinon un clic dedans ouvrirait la fiche
      const b = rowButton(() => ({...titleYear(name), alt, releases: [{name, size, approx: true, remux: /\bREMUX\b/i.test(name)}]}),
        box => a.insertAdjacentElement('afterend', box), a);
      (nameEl.closest('[class*="_rowNameRow_"]') || nameEl.parentNode).appendChild(b);
    });
  }

  // Fiche d'un torrent : IMDb/TMDB lus dans la présentation (liens « Lien IMDB / TMDB »), sinon titre + année du nom
  function tr4kerDetail() {
    const hero = document.querySelector('main [class*="_hero_"]');
    const h1 = hero && hero.querySelector('h1');
    const body = document.querySelector('main [class*="_classicBody_"]');
    if (!h1 || !body) return;                       // présentation pas encore chargée : l'observateur rappellera
    const key = (document.querySelector('main [class*="_infoHash_"]') || {}).textContent || h1.textContent;
    if (h1.dataset.pmx === key) return;             // déjà traité (React réutilise le même titre d'une fiche à l'autre)
    h1.dataset.pmx = key;
    document.querySelectorAll('main > .pmx-box').forEach(e => e.remove());
    const {name, alt} = splitAlt(h1.textContent.trim());
    const imdb = ((body.querySelector('a[href*="imdb.com/title/tt"]') || {}).href || '').match(/tt(\d+)/);
    const tmdb = ((body.querySelector('a[href*="themoviedb.org/movie/"]') || {}).href || '').match(/movie\/(\d+)/);
    const size = parseSize((hero.querySelector('[class*="_statSize_"]') || {}).textContent);
    detailPanel(hero.nextElementSibling || body, {imdb: imdb ? imdb[1] : '', tmdb: tmdb ? +tmdb[1] : null, ...titleYear(name), alt},
      [{badge: h1, name, size, approx: true, remux: /\bREMUX\b/i.test(name)}]);
  }

  function tr4ker() {
    const scan = () => { tr4kerRows(); tr4kerDetail(); };
    scan();
    let t = 0;  // recherche, pages, navigation interne : on rescanne après chaque rendu
    new MutationObserver(() => { clearTimeout(t); t = setTimeout(scan, 150); })
      .observe(document.body, {childList: true, subtree: true});
  }

  /* ------------------------------------------------------------ Nostradamus (Phoenix LiveView : la page se redessine) */
  // Catégorie lue dans l'étiquette du site (« Bluray Remux », « WEB (WEB-DL) ») ; « Bluray » seul : le nom décide.
  const nostraKind = chip => /remux/i.test(chip) ? 'remux' : /WEB-?DL/i.test(chip) ? 'webdl' : '';
  function nostraRows() {
    document.querySelectorAll('[data-torrent-list-row]').forEach(row => {
      if (row.querySelector(':scope > .pmx-slot')) return;   // déjà équipée (et toujours là après un redessin)
      const link = row.querySelector('[data-torrent-title-link]');
      if (!link) return;
      const name = link.textContent.trim();
      const size = parseSize([...row.querySelectorAll('div, span')].map(e => e.textContent)
        .find(t => /^\s*[\d.,]+\s*[KMGT]i?[oB]\s*$/i.test(t)));
      const kind = nostraKind((row.querySelector('[data-torrent-chip="metadata"]') || {}).textContent || '');
      // un emplacement à soi, au-dessus du lien qui couvre toute la ligne : visible en vue mobile comme en vue large
      const slot = document.createElement('div');
      slot.className = 'pmx-slot';
      slot.style.cssText = 'position:relative;z-index:20;pointer-events:auto;padding:0 12px 6px';
      const b = rowButton(() => ({...titleYear(name), releases: [{name, size, approx: true, kind,
        remux: kind === 'remux' || /\bREMUX\b/i.test(name)}]}), box => slot.appendChild(box), row);
      slot.appendChild(b);
      row.appendChild(slot);
    });
  }
  function nostraDetail() {
    const h1 = document.getElementById('torrent-display-title');
    const card = document.getElementById('torrent-top-card');
    if (!h1 || !card) return;
    const name = h1.textContent.trim();
    if (document.getElementById('pmx-detail') && document.getElementById('pmx-detail').dataset.name === name) return;
    document.querySelectorAll('#pmx-detail').forEach(e => e.remove());
    const tmdb = ((card.querySelector('a[href*="themoviedb.org/movie/"]') || {}).href || '').match(/movie\/(\d+)/);
    const imdb = ((card.querySelector('a[href*="imdb.com/title/tt"]') || {}).href || '').match(/tt(\d+)/);
    const label = txt => { const p = [...card.querySelectorAll('p')].find(e => e.textContent.trim().toLowerCase() === txt);
      return p && p.nextElementSibling ? p.nextElementSibling.textContent : ''; };
    const qual = [...card.querySelectorAll('span')].find(e => /^\s*Qualité\s*:\s*$/i.test(e.textContent));
    const kind = nostraKind(qual && qual.nextElementSibling ? qual.nextElementSibling.textContent : '');
    const anchor = document.createElement('div');  // repère pour placer le cadre juste sous la carte du torrent
    card.insertAdjacentElement('afterend', anchor);
    detailPanel(anchor, {imdb: imdb ? imdb[1] : '', tmdb: tmdb ? +tmdb[1] : null, ...titleYear(name)},
      [{badge: h1, name, size: parseSize(label('taille')), approx: true, kind, remux: kind === 'remux' || /\bREMUX\b/i.test(name)}]);
    const box = anchor.previousElementSibling;
    if (box) { box.id = 'pmx-detail'; box.dataset.name = name; }
    anchor.remove();
  }
  function nostradamus() {
    const scan = () => { nostraRows(); nostraDetail(); };
    scan();
    let t = 0;
    new MutationObserver(() => { clearTimeout(t); t = setTimeout(scan, 200); }).observe(document.body, {childList: true, subtree: true});
  }

  /* ------------------------------------------------------------ C411 (application Nuxt) */
  // Liste : une ligne par film (titre français + année), releases repliées et sans nom : on dit seulement si le film
  // est sur HDF et dans quels formats. Le verdict de repost se lit sur la fiche d'un torrent (nom, taille, TMDB).
  function c411Rows() {
    document.querySelectorAll('div.cursor-pointer.select-none').forEach(head => {
      const img = head.querySelector('img[src*="image.tmdb.org/t/p/"]');
      const group = head.parentNode;
      if (!img || !group || group.querySelector(':scope > .pmx-slot')) return;
      const title = (img.getAttribute('alt') || '').trim();
      const year = ((head.textContent.match(/\((\d{4})\)/) || [])[1]) || '';
      if (!title) return;
      const slot = document.createElement('div');
      slot.className = 'pmx-slot';
      slot.style.cssText = 'padding:0 16px 6px';
      slot.addEventListener('click', e => e.stopPropagation());  // ne pas déplier la ligne
      const b = rowButton(() => ({title, year, releases: []}), box => slot.appendChild(box), group);
      slot.appendChild(b);
      head.insertAdjacentElement('afterend', slot);
    });
  }
  function c411Detail() {
    const h1 = document.querySelector('main h1');
    const card = h1 && h1.closest('[data-slot="root"]');
    const links = [...document.querySelectorAll('main a[href*="themoviedb.org/movie/"], main a[href*="imdb.com/title/tt"]')];
    if (!card || !links.length) return;
    const name = h1.textContent.replace(/\s+/g, '').trim();  // le nom est coupé par des <wbr>
    if (document.getElementById('pmx-detail') && document.getElementById('pmx-detail').dataset.name === name) return;
    document.querySelectorAll('#pmx-detail').forEach(e => e.remove());
    const tmdb = ((links.find(a => /themoviedb/.test(a.href)) || {}).href || '').match(/movie\/(\d+)/);
    const imdb = ((links.find(a => /imdb/.test(a.href)) || {}).href || '').match(/tt(\d+)/);
    const sizeEl = [...card.querySelectorAll('span')].find(e => /^\s*[\d.,]+\s*[KMGT]i?[oB]\s*$/i.test(e.textContent));
    const anchor = document.createElement('div');
    card.insertAdjacentElement('afterend', anchor);
    detailPanel(anchor, {imdb: imdb ? imdb[1] : '', tmdb: tmdb ? +tmdb[1] : null, ...titleYear(name)},
      [{badge: h1, name, size: parseSize(sizeEl && sizeEl.textContent), approx: true, remux: /\bREMUX\b/i.test(name)}]);
    const box = anchor.previousElementSibling;
    if (box) { box.id = 'pmx-detail'; box.dataset.name = name; }
    anchor.remove();
  }
  function c411() {
    const scan = () => { c411Rows(); c411Detail(); };
    scan();
    let t = 0;
    new MutationObserver(() => { clearTimeout(t); t = setTimeout(scan, 200); }).observe(document.body, {childList: true, subtree: true});
  }

  if (location.hostname.includes('passthepopcorn')) ptpDetail() || ptpList();
  else if (location.hostname.includes('abn')) abnDetail() || abnList();
  else if (location.hostname.includes('tr4ker')) tr4ker();
  else if (location.hostname.includes('nostradamus')) nostradamus();
  else if (location.hostname.includes('c411')) c411();
})();
