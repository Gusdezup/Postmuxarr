// ==UserScript==
// @name         Postmuxarr → HD-Forever
// @namespace    postmuxarr
// @version      3.4
// @description  Remplit le formulaire d'upload de HDF depuis Postmuxarr (remux et reposts : Blu-ray Original, remux, rip, mHD, WEB-DL) : TMDB, .torrent, MediaInfo/BDInfo, catégorie, langues, sous-titres, versions. L'envoi reste manuel.
// @match        https://hdf.world/*
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @grant        GM_setClipboard
// @grant        GM.setClipboard
// @connect      __POSTMUXARR_HOST__
// @updateURL    __SELF_URL__
// @downloadURL  __SELF_URL__
// @inject-into  page
// @run-at       document-idle
// ==/UserScript==

(() => {
  'use strict';
  const POSTMUXARR = '__POSTMUXARR_URL__';  // rempli par Postmuxarr à l'installation (Réglages ▸ Userscripts)
  const API_KEY = '__API_KEY__';           // vide si la protection par mot de passe est désactivée
  const LANGS = ['VFI', 'VFF', 'VFQ', 'VO', 'VOF', 'VOQ'];
  // la vraie fenêtre de la page : avec des @grant, « window » est un proxy de Violentmonkey qui ne voit pas jQuery
  const W = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
  // Compatibilité : API GM_* (Violentmonkey, Tampermonkey) ou GM.* (Greasemonkey 4, Userscripts pour Safari)
  const GMX = typeof GM_xmlhttpRequest === 'function' ? GM_xmlhttpRequest
    : typeof GM !== 'undefined' && GM && typeof GM.xmlHttpRequest === 'function' ? o => GM.xmlHttpRequest(o) : null;
  const xhr = (reject, o) => GMX ? GMX(o)
    : reject(new Error('Ce gestionnaire de userscripts ne fournit pas GM_xmlhttpRequest : utilise Violentmonkey ou Tampermonkey'));
  // release en cours d'envoi : localStorage de hdf.world (même origine que upload.php), pour tous les gestionnaires
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
    del(k) { try { localStorage.removeItem(k); } catch (e) {} },
  };
  function gmClipboard(text) {
    if (typeof GM_setClipboard === 'function') return GM_setClipboard(text, 'text');
    if (typeof GM !== 'undefined' && GM && typeof GM.setClipboard === 'function') return GM.setClipboard(text);
    throw new Error('presse-papiers indisponible');
  }

  const $ = s => document.querySelector(s);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  async function waitFor(fn, ms) {
    const end = Date.now() + ms;
    while (Date.now() < end) { try { if (fn()) return true; } catch (e) {} await sleep(300); }
    return false;
  }
  function api(path, method = 'GET') {
    return new Promise((resolve, reject) => xhr(reject, {
      method, url: POSTMUXARR + path, timeout: 180000, headers: API_KEY ? {'X-Api-Key': API_KEY} : {},
      onload: r => { let j = null; try { j = JSON.parse(r.responseText); } catch (e) {}
        if (r.status === 401) return reject(new Error('Postmuxarr refuse l\'accès : réinstalle le userscript depuis Réglages ▸ Userscripts'));
        r.status < 400 ? resolve(j) : reject(new Error((j && j.detail) || `Postmuxarr : HTTP ${r.status}`)); },
      onerror: () => reject(new Error(`Postmuxarr injoignable (${POSTMUXARR})`)),
      ontimeout: () => reject(new Error('Postmuxarr : délai dépassé')),
    }));
  }
  const fire = (el, ...types) => types.forEach(t => el.dispatchEvent(new Event(t, {bubbles: true})));
  function setVal(el, v) { if (el) { el.value = v; fire(el, 'input', 'change'); } }
  function pick(sel, v) {  // ne remplace que ce que le site n'a pas déduit lui-même
    const el = $(sel);
    if (el && v && (!el.value || el.value === '---') && [...el.options].some(o => o.value === v)) setVal(el, v);
  }
  function check(el, on) { if (el && el.checked !== on) { el.checked = on; fire(el, 'change'); } }

  /* ------------------------------------------------------------ après l'envoi */
  // Au clic sur « Uploader », la release est notée. Si la page suivante n'est plus upload.php, le site a accepté
  // l'upload : Postmuxarr réannonce alors jusqu'au feu vert du tracker. Retour sur upload.php = refus, rien à signaler.
  const PENDING = 'postmuxarr_pending';
  if (location.pathname !== '/upload.php') {
    const p = store.get(PENDING);
    if (!p) return;
    store.del(PENDING);
    if (Date.now() - p.at > 10 * 60 * 1000) return;
    const note = document.createElement('div');
    note.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:99999;background:#171e36;color:#e7e9f3;'
      + 'border:1px solid #f3b443;border-radius:8px;padding:8px 12px;font:13px system-ui,sans-serif';
    document.body.appendChild(note);
    api(`/api/hdf/${p.id}/uploaded`, 'POST')
      .then(() => { note.textContent = 'Postmuxarr : upload accepté, reannounce lancé.'; })
      .catch(e => { note.textContent = 'Postmuxarr : ' + e.message + ' (bouton « Réannoncer » du job)'; note.style.borderColor = '#ec6b6b'; });
    setTimeout(() => note.remove(), 8000);
    return;
  }
  store.del(PENDING);
  let current = null;
  const form = document.getElementById('upload_table');
  if (form) form.addEventListener('submit', () => { if (current) store.set(PENDING, {id: current, at: Date.now()}); }, true);

  /* ------------------------------------------------------------ panneau */
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;top:12px;right:12px;z-index:99999;width:340px;max-height:80vh;overflow:auto;'
    + 'background:#171e36;color:#e7e9f3;border:1px solid #f3b443;border-radius:10px;padding:12px 14px;'
    + 'font:13px/1.45 system-ui,sans-serif;box-shadow:0 6px 24px #0008';
  box.innerHTML = '<div style="display:flex;justify-content:space-between;margin-bottom:6px"><b style="color:#f3b443">Postmuxarr</b>'
    + '<span id="rmx-x" style="cursor:pointer;color:#8d95b8">✕</span></div><div id="rmx-log"></div>';
  document.body.appendChild(box);
  box.querySelector('#rmx-x').onclick = () => box.remove();
  const out = box.querySelector('#rmx-log');
  function log(msg, kind) {
    const p = document.createElement('div');
    p.textContent = msg;
    if (kind) p.style.color = {ok: '#67c99a', warn: '#f3b443', err: '#ec6b6b'}[kind];
    if (kind === 'mono') p.style.cssText = 'font:11px ui-monospace,monospace;word-break:break-all;margin:2px 0 6px';
    out.appendChild(p);
    box.scrollTop = box.scrollHeight;
    return p;
  }

  /* ------------------------------------------------------------ remplissage */
  /* ------------------------------------------------------------ brouillons du site */
  // autosave.js garde le MediaInfo du dernier upload (clé « torrent_release ») et propose de le restaurer :
  // ce brouillon prend la main sur le champ. On l'efface au chargement et on ferme sa bannière.
  function clearDrafts() {
    try {
      Object.keys(localStorage).filter(k => /torrent_release|release_desc|autosave/i.test(k)).forEach(k => localStorage.removeItem(k));
    } catch (e) {}
  }
  function dismissDraftBanner() {
    const b = [...document.querySelectorAll('button, input[type=button], a')]
      .find(x => /^\s*Ignorer\s*$/.test(x.textContent || x.value || ''));
    if (b) { b.click(); return true; }
    return false;
  }

  // Contenu actuel du MediaInfo, quel que soit le mode de l'éditeur (visuel ou [BBCODE])
  function currentBB() {
    const ta = $('#release_desc'), jq = W.jQuery, wbb = jq && jq('#release_desc').data('wbb');
    try { return (wbb ? wbb.getBBCode() : ta.value) || ''; } catch (e) { return ta.value || ''; }
  }

  // Même méthode que le bouton « Restaurer » du site (autosave.js → setContent), qui fonctionne chez toi :
  // pas de bascule de mode, on remplit l'éditeur visuel et le textarea caché, puis on synchronise.
  async function setMediainfo(text, trace) {
    text = text.replace(/\r\n?/g, '\n');
    const t = trace || [];
    const ta = $('#release_desc'), jq = W.jQuery, wbb = jq && jq('#release_desc').data('wbb');
    const len = () => `ta=${ta.value.length} vis=${wbb && wbb.$body ? wbb.$body.text().length : '-'} bb=${currentBB().length}`;
    t.push(`ctx=${(typeof GM_info !== 'undefined' && GM_info.injectInto) || '?'} jq=${typeof jq} wbb=${typeof wbb} mode=${wbb ? (wbb.options.bbmode ? 'bb' : 'visuel') : '-'} txt=${text.length}`);
    if (!wbb) { setVal(ta, text); t.push('sans wbb : ' + len()); return currentBB().trim().length > 50 ? 'champ texte' : ''; }
    ta.value = text;
    t.push('1 ' + len());
    if (!wbb.options.bbmode && wbb.$body) {
      let html = '';
      try { html = wbb.getHTML(text, true); } catch (e) { t.push('getHTML erreur : ' + e.message); }
      t.push('html=' + html.length);
      wbb.$body.html(html);
      wbb.lastRange = null;
      t.push('2 ' + len());
    }
    wbb.sync();
    t.push('3 ' + len());
    await sleep(300);
    t.push('4 ' + len());
    return currentBB().trim().length > 50 ? (wbb.options.bbmode ? 'mode BBCode' : 'comme « Restaurer »') : '';
  }

  async function pasteMediainfo(text) {
    const lost = () => currentBB().trim().length < 50;
    try {
      const trace = [];
      let how = await setMediainfo(text, trace), fixes = 0;

      // gardien : pendant ~6 s, si le site vide le champ ou repasse en mode visuel, on recolle
      for (let i = 0; how && i < 20; i++) {
        await sleep(300);
        if (!lost()) continue;
        if (++fixes > 5) break;
        dismissDraftBanner();
        how = await setMediainfo(text);
      }
      if (how && !lost()) log(`MediaInfo collé (${how}${fixes ? `, recollé ${fixes} fois` : ''}).`, 'ok');
      else { log('MediaInfo effacé par le site : « Copier » puis ⌘V dans le champ.', 'err'); log(trace.join(' | '), 'mono'); }
    } catch (e) { log('MediaInfo : ' + e.message, 'err'); }
  }

  function mediainfoButtons(text) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:6px;margin:8px 0 4px';
    const btn = (label, fn) => {
      const b = document.createElement('button');
      b.textContent = label;
      b.style.cssText = 'flex:1;padding:4px 6px;background:#1f2846;color:inherit;border:1px solid #2b3558;border-radius:6px;cursor:pointer;font:12px system-ui,sans-serif';
      b.onclick = fn;
      row.appendChild(b);
    };
    btn('Recoller le MediaInfo', () => pasteMediainfo(text));
    btn('Copier le MediaInfo', async () => {
      try { await navigator.clipboard.writeText(text); }
      catch (e) { try { gmClipboard(text); } catch (e2) { return log('Copie impossible : sélectionne le texte du champ à la main.', 'err'); } }
      log('MediaInfo copié : clique dans le champ, puis ⌘V.', 'ok');
    });
    out.appendChild(row);
  }

  async function fill(id) {
    current = id;
    out.innerHTML = '';
    let d;
    try { log('Lecture de la release…'); d = await api('/api/hdf/' + id); }
    catch (e) { return log(e.message, 'err'); }
    out.innerHTML = '';
    log(d.name, 'mono');
    if (await waitFor(dismissDraftBanner, 2500)) log('Brouillon du site ignoré.');

    // 1. TMDB : le site retrouve le film (existant → ajout de format) et remplit titre, année, artistes, affiche, genres
    const titled = () => $('#title') && $('#title').value.trim();
    if (d.tmdb_url) {
      setVal($('#allocine_url'), d.tmdb_url);
      $('#btnAllocineFetch').click();
      log('TMDB envoyé, attente du site…');
      if (await waitFor(titled, 30000)) log('Fiche TMDB chargée.', 'ok');
      else log('Le site n\'a pas rempli le titre : vérifie la partie TMDB.', 'warn');
    } else {
      log('Pas de TMDB enregistré pour cette release : colle l\'URL TMDB et clique sur « Envoyer », j\'attends.', 'warn');
      if (!(await waitFor(titled, 300000))) return log('Abandon : fiche TMDB non chargée.', 'err');
      log('Fiche TMDB chargée.', 'ok');
    }
    await sleep(500);

    // 2. .torrent (le site en déduit codec, résolution, type de fichier et team)
    const bin = Uint8Array.from(atob(d.torrent), c => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bin], d.torrent_name, {type: 'application/x-bittorrent'}));
    const fi = $('#file');
    fi.files = dt.files;
    fire(fi, 'change');
    log('.torrent joint.', 'ok');
    await sleep(800);

    // 3. ce que le site n'aurait pas déduit
    pick('#format', d.codec); pick('#bitrate', d.res); pick('#media', d.media);
    const media = $('#media');
    if (d.media && media && media.value !== d.media) log(`Catégorie « ${d.media} » non sélectionnée : choisis-la dans « Média ».`, 'warn');
    else if (!d.media) log('Catégorie inconnue : choisis-la dans « Média ».', 'warn');
    const team = $('#team');
    if (team && !team.value.trim() && d.team) setVal(team, d.team);

    // 4. langues, sous-titres, versions
    LANGS.forEach(k => check(document.getElementById(k), d.langs.includes(k)));
    check($('#SRT'), d.srt);
    d.versions.forEach(v => check(document.getElementById('version-' + v), true));

    // 5. MediaInfo
    await pasteMediainfo(d.mediainfo);

    log(`Média : ${d.media || '?'} · Langues : ${d.langs.join(', ') || 'aucune'} · Sous-titres FR : ${d.srt ? 'oui' : 'non'}`
      + ` · Versions : ${d.versions.join(', ') || 'aucune'}`);
    if (!d.langs.length) log('Aucune langue détectée : coche-la toi-même.', 'warn');
    if (d.lang_note) log(d.lang_note, 'warn');
    log('Vérifie le formulaire, puis clique sur « Uploader ».', 'ok');
    mediainfoButtons(d.mediainfo);
  }

  /* ------------------------------------------------------------ démarrage */
  (async () => {
    clearDrafts();
    const id = (location.hash.match(/(?:postmuxarr|remuxer)=(\d+)/) || [])[1];
    if (id) return fill(id);
    try {
      const list = await api('/api/hdf');
      if (!list.length) return log('Aucune release avec .torrent dans Postmuxarr.');
      log('Remplir avec :');
      list.forEach(r => {
        const b = document.createElement('button');
        b.textContent = r.name + (r.seed === 'ok' ? '  ✓ déjà en seed' : '');
        b.style.cssText = 'display:block;width:100%;text-align:left;margin:4px 0;padding:4px 6px;background:#1f2846;'
          + 'color:inherit;border:1px solid #2b3558;border-radius:6px;cursor:pointer;word-break:break-all;'
          + 'font:11px ui-monospace,monospace' + (r.seed === 'ok' ? ';opacity:.5' : '');
        b.onclick = () => fill(r.id);
        out.appendChild(b);
      });
    } catch (e) { log(e.message, 'err'); }
  })();
})();
