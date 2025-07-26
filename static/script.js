/*
Mode d'emploi:
Description: Script JavaScript pour l'interface de configuration Caddyfile (Table/Modal UI).
Fonctionnalités:
    - Onglet Configurator:
        - Affiche les sites dans un tableau trié.
        - Permet d'ajouter/modifier/supprimer des sites via un modal.
        - Chaque modification génère, sauvegarde le Caddyfile et recharge Caddy automatiquement.
    - Onglet Raw Caddyfile:
        - Affiche le Caddyfile généré dans une zone de texte éditable.
        - Permet de parser le texte brut pour mettre à jour les données des sites et le tableau.
        - Permet d'importer/exporter le Caddyfile.
    - Onglet Preferences:
        - Gère les options globales (email) et les valeurs par défaut.
        - Sauvegarde/Charge les préférences via l'API backend.
Choix techniques:
    - JavaScript Vanilla.
    - API Fetch pour la communication backend.
    - Persistance des préférences côté serveur.
    - Stockage des configurations de site dans an array JS (`siteConfigs`).
    - Rendu dynamique du tableau et du modal.
    - Parsing/Génération Caddyfile multi-site côté client (best-effort).
    - Thème stocké dans localStorage pour persistance entre pages.
*/

// --- DOM Elements ---
const toastContainer = document.getElementById('toast-container');

const outputTextarea = document.getElementById('caddyfile-output');
const themeSelect = document.getElementById('theme-select');
const savePrefsBtn = document.getElementById('save-prefs-btn');
const prefsForm = document.getElementById('prefs-form');

// Raw Caddyfile Tab Elements
const parseRawBtn = document.getElementById('parse-raw-btn');
const exportCaddyfileBtn = document.getElementById('export-caddyfile-btn');
const importCaddyfileBtn = document.getElementById('import-caddyfile-btn');
const importCaddyfileInput = document.getElementById('import-caddyfile-input');

// Configurator Tab Elements
const addHostBtn = document.getElementById('add-host-btn');
const sitesTableBody = document.getElementById('sites-table-body');
// Modal Elements
const siteModal = document.getElementById('site-modal');
const modalTitle = document.getElementById('modal-title');
const modalForm = document.getElementById('modal-form');
const modalSiteIndexInput = document.getElementById('modal-site-index');
const modalSaveBtn = document.getElementById('modal-save-btn');
const modalCancelBtn = document.getElementById('modal-cancel-btn');
const closeModalBtn = siteModal ? siteModal.querySelector('.close-modal-btn') : null;
// Preference Form Fields
const globalAdminEmailInput = document.getElementById('global-admin-email');
const defaultSkipTlsVerifyInput = document.getElementById('default-skip-tls-verify');
const defaultAuthentikEnabledInput = document.getElementById('default-authentik-enabled');
const defaultAuthentikOutpostUrlInput = document.getElementById('default-authentik-outpost-url');
const defaultAuthentikUriInput = document.getElementById('default-authentik-uri');
const defaultAuthentikCopyHeadersInput = document.getElementById('default-authentik-copy-headers');
const defaultAuthentikTrustedProxiesInput = document.getElementById('default-authentik-trusted-proxies');

// --- Global State ---
let currentPreferences = {};
let siteConfigs = [];
let isAutoSaving = false;

// --- Toast Notification System ---
function showToast(message, type = 'info', duration = 3000) {
    if (!toastContainer) {
        console.error("Toast container not found!");
        alert(`${type.toUpperCase()}: ${message}`);
        return;
    }

    const toast = document.createElement('div');
    toast.className = `toast ${type === 'error' ? 'danger' : type}`;

    const messageSpan = document.createElement('span');
    messageSpan.className = 'toast-message';
    messageSpan.textContent = message;
    toast.appendChild(messageSpan);

    const closeButton = document.createElement('button');
    closeButton.className = 'toast-close-btn';
    closeButton.innerHTML = '×';
    closeButton.setAttribute('aria-label', 'Close');
    closeButton.onclick = () => {
        toast.classList.remove('show');
        setTimeout(() => {
            if (toast.parentNode === toastContainer) {
                 toastContainer.removeChild(toast);
            }
        }, 500);
    };
    toast.appendChild(closeButton);
    toastContainer.appendChild(toast);
    setTimeout(() => toast.classList.add('show'), 10);
    setTimeout(() => { if (toast.classList.contains('show')) closeButton.click(); }, duration);
}


// --- Event Listeners ---
if (themeSelect) {
    themeSelect.addEventListener('change', () => applyTheme(themeSelect.value));
}
if (savePrefsBtn) {
    savePrefsBtn.addEventListener('click', savePreferences);
}
if (addHostBtn) {
    addHostBtn.addEventListener('click', () => openSiteModal());
}
if (parseRawBtn) {
    parseRawBtn.addEventListener('click', handleParseRawTextAndAutoSave);
}
if (exportCaddyfileBtn) {
    exportCaddyfileBtn.addEventListener('click', handleExportCaddyfile);
}
if (importCaddyfileBtn) {
    importCaddyfileBtn.addEventListener('click', () => importCaddyfileInput.click());
}
if (importCaddyfileInput) {
    importCaddyfileInput.addEventListener('change', handleImportCaddyfile);
}
if (sitesTableBody) {
    sitesTableBody.addEventListener('click', handleTableActionClick);
    sitesTableBody.addEventListener('change', handleTableCheckboxChange);
}
// Modal Listeners
if (closeModalBtn) closeModalBtn.addEventListener('click', closeSiteModal);
if (modalCancelBtn) modalCancelBtn.addEventListener('click', closeSiteModal);
if (modalSaveBtn) modalSaveBtn.addEventListener('click', saveSiteFromModal);
if (siteModal) {
    siteModal.addEventListener('click', (event) => { if (event.target === siteModal) closeSiteModal(); });
    const modalContent = siteModal.querySelector('.modal-content');
    if (modalContent) modalContent.addEventListener('click', handleModalToggleClick);
    const modalAuthCheckbox = document.getElementById('modal-authentik-enable');
    if (modalAuthCheckbox) modalAuthCheckbox.addEventListener('change', handleModalAuthentikCheckboxChange);
}

// --- Initialization ---
document.addEventListener('DOMContentLoaded', async () => {
    // Appliquer le thème stocké en priorité, sinon celui du select, sinon le défaut CSS.
    const initialStoredTheme = localStorage.getItem('caddyPanelTheme');
    if (initialStoredTheme) {
        if (themeSelect) themeSelect.value = initialStoredTheme; // Mettre à jour le select si possible
        applyTheme(initialStoredTheme); // applyTheme va aussi mettre à jour localStorage
    } else if (themeSelect) {
        // Si aucun thème stocké, appliquer la valeur par défaut du select (et la stocker)
        applyTheme(themeSelect.value);
    }
    
    await loadPreferences(); // loadPreferences va aussi gérer l'application du thème basé sur les préférences ou localStorage
    
    const initialDataScript = document.getElementById('initial-data');
    let initialContent = null;
    if (initialDataScript) {
        try { 
            const initialData = JSON.parse(initialDataScript.textContent); 
            initialContent = initialData.initial_caddyfile_content; 
        }
        catch (e) { console.error("Error parsing initial data:", e); }
    }

    if (initialContent) {
        if (typeof parseCaddyfile === 'function') {
            parseCaddyfile(initialContent);
            if (outputTextarea) outputTextarea.value = initialContent;
        } else { 
            console.error("parseCaddyfile function not found."); 
            renderSitesTable();
            if (typeof generateCaddyfileFromData === 'function') generateCaddyfileFromData();
        }
    } else {
        renderSitesTable();
        if (typeof generateCaddyfileFromData === 'function') generateCaddyfileFromData();
    }
});


// --- Site Data Management & Table Rendering ---
function renderSitesTable() {
    if (!sitesTableBody) return;
    sitesTableBody.innerHTML = '';
    if (siteConfigs.length === 0) {
        sitesTableBody.innerHTML = '<tr><td colspan="6">No sites configured. Click "Add Proxy Host".</td></tr>';
        return;
    }
    sortSiteConfigs();
    siteConfigs.forEach((site, index) => {
        const row = sitesTableBody.insertRow();
        row.dataset.index = index;

        const cellHost = row.insertCell();
        const cellForward = row.insertCell();
        const cellSSL = row.insertCell();
        const cellSkipTls = row.insertCell();
        const cellAuthentik = row.insertCell();
        const cellActions = row.insertCell();

        const siteLink = document.createElement('a');
        if (site.address) {
            let href = site.address;
            if (!href.startsWith('http://') && !href.startsWith('https://')) {
                if (href.includes(':') || href === '*' || href.startsWith('*.')) {
                    href = `http://${href.replace(/^\*./, 'www.')}`; 
                } else {
                    href = `https://${href}`;
                }
            }
            siteLink.href = href;
            siteLink.textContent = site.address;
        } else {
            siteLink.textContent = 'N/A';
            siteLink.href = '#';
        }
        siteLink.target = '_blank';
        cellHost.appendChild(siteLink);

        if (site.reverse_proxy) {
            const proxyLink = document.createElement('a');
            let rpUrl = site.reverse_proxy;
            if (!rpUrl.startsWith('http://') && !rpUrl.startsWith('https://')) {
                rpUrl = `http://${rpUrl}`;
            }
            proxyLink.href = rpUrl;
            proxyLink.textContent = site.reverse_proxy;
            proxyLink.target = '_blank';
            cellForward.appendChild(proxyLink);
        } else {
            cellForward.textContent = '-';
        }

        let sslStatus = 'Auto';
        if (site.tls === 'internal') sslStatus = 'Internal';
        else if (site.tls === 'off') sslStatus = 'Off';
        cellSSL.textContent = sslStatus;

        const skipTlsCheckbox = document.createElement('input');
        skipTlsCheckbox.type = 'checkbox';
        skipTlsCheckbox.checked = site.tls_skip_verify || false;
        skipTlsCheckbox.dataset.index = index;
        skipTlsCheckbox.dataset.action = 'toggle-skip-tls';
        skipTlsCheckbox.title = 'Toggle Skip TLS Verify for reverse_proxy target';
        cellSkipTls.appendChild(skipTlsCheckbox);
        cellSkipTls.style.textAlign = 'center';

        const authentikCheckbox = document.createElement('input');
        authentikCheckbox.type = 'checkbox';
        authentikCheckbox.checked = !!site.forward_auth;
        authentikCheckbox.dataset.index = index;
        authentikCheckbox.dataset.action = 'toggle-authentik';
        authentikCheckbox.title = 'Toggle Authentik Integration';
        cellAuthentik.appendChild(authentikCheckbox);
        cellAuthentik.style.textAlign = 'center';

        cellActions.classList.add('actions');
        cellActions.innerHTML = `
            <button type="button" class="action-btn stats-btn" title="View Stats (placeholder)">📊</button>
            <button type="button" class="action-btn edit-btn" title="Edit">✏️</button>
            <button type="button" class="action-btn delete-btn" title="Delete">❌</button>
        `;
    });
}

function sortSiteConfigs() { siteConfigs.sort((a, b) => (a.address || '').localeCompare(b.address || '')); }

async function handleTableActionClick(event) {
    const target = event.target.closest('button.action-btn');
    if (!target) return;

    const row = target.closest('tr');
    if (!row || !row.parentNode || row.parentNode.tagName !== 'TBODY') { return; }
    const index = parseInt(row.dataset.index, 10);
    if (isNaN(index) || index < 0 || index >= siteConfigs.length) {
        console.error("Invalid index for table action:", row.dataset.index);
        return;
    }

    if (target.classList.contains('edit-btn')) {
        openSiteModal(index);
    } else if (target.classList.contains('delete-btn')) {
        if (confirm(`Are you sure you want to delete site configuration for "${siteConfigs[index]?.address || 'this site'}"?`)) {
            siteConfigs.splice(index, 1);
            renderSitesTable(); 
            generateCaddyfileFromData();
            await autoSaveAndReloadCaddy();
        }
    } else if (target.classList.contains('stats-btn')) {
        showToast(`Statistics for "${siteConfigs[index]?.address || 'this site'}" are not yet implemented.`, "info");
    }
}

async function handleTableCheckboxChange(event) {
    const target = event.target;
    if (target.type !== 'checkbox' || !target.dataset.action) return;

    const row = target.closest('tr');
     if (!row || !row.parentNode || row.parentNode.tagName !== 'TBODY') return;
    const index = parseInt(target.dataset.index, 10); 

    if (isNaN(index) || index < 0 || index >= siteConfigs.length) {
        console.error("Invalid index for table checkbox change:", target.dataset.index);
        return;
    }

    const action = target.dataset.action;
    let siteChanged = false;

    if (action === 'toggle-skip-tls') {
        siteConfigs[index].tls_skip_verify = target.checked;
        siteChanged = true;
    } else if (action === 'toggle-authentik') {
        if (target.checked) {
            if (!siteConfigs[index].forward_auth) {
                siteConfigs[index].forward_auth = {
                    outpost_url: currentPreferences.defaultAuthentikOutpostUrl || '',
                    uri: currentPreferences.defaultAuthentikUri || '/outpost.goauthentik.io/auth/caddy',
                    copy_headers: currentPreferences.defaultAuthentikCopyHeaders || 'X-Authentik-Username X-Authentik-Groups X-Authentik-Email X-Authentik-Name X-Authentik-Uid X-Authentik-Jwt X-Authentik-Meta-Jwks',
                    trusted_proxies: currentPreferences.defaultAuthentikTrustedProxies || 'private_ranges'
                };
            }
        } else {
            siteConfigs[index].forward_auth = null;
        }
        siteChanged = true;
    }

    if (siteChanged) {
        generateCaddyfileFromData();
        await autoSaveAndReloadCaddy();
    }
}


// --- Modal Management ---
function openSiteModal(index = -1) {
    if (!siteModal || !modalForm) return;
    modalForm.reset(); 
    modalSiteIndexInput.value = index; 
    let siteData = {};

    if (index >= 0 && index < siteConfigs.length) { 
        siteData = JSON.parse(JSON.stringify(siteConfigs[index])); 
        modalTitle.textContent = `Edit Proxy Host: ${siteData.address || ''}`; 
    } else { 
        modalTitle.textContent = 'Add Proxy Host'; 
        siteData.tls_skip_verify = currentPreferences.defaultSkipTlsVerify || false; 
        if (currentPreferences.defaultAuthentikEnabled) { 
            siteData.forward_auth = { 
                outpost_url: currentPreferences.defaultAuthentikOutpostUrl || '', 
                uri: currentPreferences.defaultAuthentikUri || '/outpost.goauthentik.io/auth/caddy', 
                copy_headers: currentPreferences.defaultAuthentikCopyHeaders || 'X-Authentik-Username X-Authentik-Groups X-Authentik-Email X-Authentik-Name X-Authentik-Uid X-Authentik-Jwt X-Authentik-Meta-Jwks', 
                trusted_proxies: currentPreferences.defaultAuthentikTrustedProxies || 'private_ranges' 
            }; 
        } else {
            siteData.forward_auth = null;
        }
        siteData.tls = 'auto';
    }
    populateModal(siteData); 
    siteModal.style.display = 'block';
}

function closeSiteModal() { if (siteModal) siteModal.style.display = 'none'; }

function populateModal(data) {
    document.getElementById('modal-server-address').value = data.address || ''; 
    document.getElementById('modal-reverse-proxy').value = data.reverse_proxy || ''; 
    document.getElementById('modal-tls-skip-verify').checked = data.tls_skip_verify || false; 
    document.getElementById('modal-root-dir').value = data.root || ''; 
    document.getElementById('modal-file-server').checked = data.file_server || false; 
    document.getElementById('modal-tls-mode').value = data.tls || 'auto'; 
    document.getElementById('modal-log-output').value = data.log || '';
    
    const authCheckbox = document.getElementById('modal-authentik-enable'); 
    const authOptions = siteModal.querySelectorAll('.modal-authentik-options');

    if (data.forward_auth) { 
        authCheckbox.checked = true; 
        document.getElementById('modal-authentik-outpost-url').value = data.forward_auth.outpost_url || ''; 
        document.getElementById('modal-authentik-uri').value = data.forward_auth.uri || '/outpost.goauthentik.io/auth/caddy'; 
        document.getElementById('modal-authentik-copy-headers').value = data.forward_auth.copy_headers || 'X-Authentik-Username X-Authentik-Groups X-Authentik-Email X-Authentik-Name X-Authentik-Uid X-Authentik-Jwt X-Authentik-Meta-Jwks'; 
        document.getElementById('modal-authentik-trusted-proxies').value = data.forward_auth.trusted_proxies || 'private_ranges'; 
        authOptions.forEach(el => el.style.display = 'block'); 
    } else { 
        authCheckbox.checked = false; 
        document.getElementById('modal-authentik-outpost-url').value = currentPreferences.defaultAuthentikOutpostUrl || ''; 
        document.getElementById('modal-authentik-uri').value = currentPreferences.defaultAuthentikUri || '/outpost.goauthentik.io/auth/caddy'; 
        document.getElementById('modal-authentik-copy-headers').value = currentPreferences.defaultAuthentikCopyHeaders || 'X-Authentik-Username X-Authentik-Groups X-Authentik-Email X-Authentik-Name X-Authentik-Uid X-Authentik-Jwt X-Authentik-Meta-Jwks';
        document.getElementById('modal-authentik-trusted-proxies').value = currentPreferences.defaultAuthentikTrustedProxies || 'private_ranges';
        authOptions.forEach(el => el.style.display = 'none'); 
    }
    siteModal.querySelectorAll('.options-section').forEach(section => { section.style.display = 'none'; });
}

async function saveSiteFromModal() {
    if (!modalForm) return;
    const index = parseInt(modalSiteIndexInput.value, 10);
    const siteData = {
        address: document.getElementById('modal-server-address').value.trim(), 
        reverse_proxy: document.getElementById('modal-reverse-proxy').value.trim(),
        tls_skip_verify: document.getElementById('modal-tls-skip-verify').checked, 
        root: document.getElementById('modal-root-dir').value.trim(),
        file_server: document.getElementById('modal-file-server').checked, 
        tls: document.getElementById('modal-tls-mode').value,
        log: document.getElementById('modal-log-output').value.trim(), 
        forward_auth: null
    };

    if (document.getElementById('modal-authentik-enable').checked) {
        siteData.forward_auth = { 
            outpost_url: document.getElementById('modal-authentik-outpost-url').value.trim(), 
            uri: document.getElementById('modal-authentik-uri').value.trim(), 
            copy_headers: document.getElementById('modal-authentik-copy-headers').value.trim(), 
            trusted_proxies: document.getElementById('modal-authentik-trusted-proxies').value.trim() 
        };
        if (!siteData.forward_auth.outpost_url) {
            showToast("Authentik Outpost URL is required if Authentik is enabled.", "warning");
        }
    }

    if (!siteData.address) { showToast("Site Address is required.", "warning"); return; }

    if (index === -1) { 
        siteConfigs.push(siteData); 
    } else if (index >= 0 && index < siteConfigs.length) { 
        siteConfigs[index] = siteData; 
    } else { 
        console.error("Invalid index found in modal:", index); return; 
    }

    renderSitesTable();
    generateCaddyfileFromData();
    closeSiteModal();
    await autoSaveAndReloadCaddy();
}

function handleModalToggleClick(event) {
    if (event.target.classList.contains('modal-toggle-btn')) {
        event.preventDefault(); 
        const sectionClass = event.target.dataset.section; 
        const section = siteModal.querySelector(`.${sectionClass}`); 
        if (section) { 
            section.style.display = section.style.display === 'none' ? 'block' : 'none'; 
        }
    }
}

function handleModalAuthentikCheckboxChange(event) {
    const isChecked = event.target.checked; 
    const optionsDivs = siteModal.querySelectorAll('.modal-authentik-options'); 
    optionsDivs.forEach(div => { div.style.display = isChecked ? 'block' : 'none'; });

    if (isChecked) {
        const outpostUrlInput = document.getElementById('modal-authentik-outpost-url');
        const uriInput = document.getElementById('modal-authentik-uri');
        const copyHeadersInput = document.getElementById('modal-authentik-copy-headers');
        const trustedProxiesInput = document.getElementById('modal-authentik-trusted-proxies');

        if (!outpostUrlInput.value) outpostUrlInput.value = currentPreferences.defaultAuthentikOutpostUrl || '';
        if (!uriInput.value) uriInput.value = currentPreferences.defaultAuthentikUri || '/outpost.goauthentik.io/auth/caddy';
        if (!copyHeadersInput.value) copyHeadersInput.value = currentPreferences.defaultAuthentikCopyHeaders || 'X-Authentik-Username X-Authentik-Groups X-Authentik-Email X-Authentik-Name X-Authentik-Uid X-Authentik-Jwt X-Authentik-Meta-Jwks';
        if (!trustedProxiesInput.value) trustedProxiesInput.value = currentPreferences.defaultAuthentikTrustedProxies || 'private_ranges';
    }
}

// --- Caddyfile Generation (from siteConfigs array) ---
function generateCaddyfileFromData() {
    if (!outputTextarea || !globalAdminEmailInput) { return; }
    let caddyfileContent = ''; 
    const adminEmail = globalAdminEmailInput.value.trim();
    if (adminEmail) {
        caddyfileContent += `{\n\temail ${adminEmail}\n}\n\n`;
    } else {
        caddyfileContent += `{\n\t# email admin@example.com # Uncomment and set your email for ACME\n}\n\n`;
    }

    if (siteConfigs.length === 0) {
        caddyfileContent += `# No site configurations defined.\n`;
    }

    siteConfigs.forEach(site => {
        if (!site.address) return; 
        caddyfileContent += `${site.address} {\n`;
        if (site.root) caddyfileContent += `\troot * ${site.root}\n`; 
        if (site.file_server) caddyfileContent += `\tfile_server\n`;
        
        if (site.reverse_proxy) { 
            const p = site.reverse_proxy.includes('://') ? site.reverse_proxy : `http://${site.reverse_proxy}`; 
            caddyfileContent += `\treverse_proxy ${p}`; 
            if (site.tls_skip_verify && p.startsWith('https://')) { 
                caddyfileContent += ` {\n\t\ttransport http {\n\t\t\ttls_insecure_skip_verify\n\t\t}\n\t}\n`; 
            } else { 
                caddyfileContent += `\n`; 
            } 
        }

        if (site.tls && site.tls !== 'auto') { 
            caddyfileContent += `\ttls ${site.tls}\n`; 
        }

        if (site.log) { 
            caddyfileContent += `\tlog {\n\t\toutput ${site.log}\n\t}\n`; 
        }
        
        if (site.forward_auth && site.forward_auth.outpost_url) { 
            caddyfileContent += `\n\t# --- Authentik Configuration --- #\n`;
            caddyfileContent += `\tforward_auth ${site.forward_auth.outpost_url} {\n`;
            if (site.forward_auth.uri) caddyfileContent += `\t\turi ${site.forward_auth.uri}\n`;
            if (site.forward_auth.copy_headers) caddyfileContent += `\t\tcopy_headers ${site.forward_auth.copy_headers}\n`;
            if (site.forward_auth.trusted_proxies) caddyfileContent += `\t\ttrusted_proxies ${site.forward_auth.trusted_proxies}\n`;
            caddyfileContent += `\t}\n`;
        } else if (site.forward_auth) { 
            caddyfileContent += `\n\t# --- Authentik Configuration (Enabled but Outpost URL missing or invalid) --- #\n`;
        }
        caddyfileContent += `}\n\n`;
    });
    outputTextarea.value = caddyfileContent.trim();
}

// --- Auto Save Caddyfile to Server and Reload Caddy ---
async function autoSaveAndReloadCaddy() {
    if (isAutoSaving) { return; }
    if (!outputTextarea) { return; }
    isAutoSaving = true;
    const caddyfileContent = outputTextarea.value;
    showToast("Auto-saving Caddyfile...", "info", 10000);
    try {
        const saveResponse = await fetch('/api/caddyfile/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: caddyfileContent }) });
        const saveData = await saveResponse.json();
        if (!saveResponse.ok || saveData.status !== 'success') { throw new Error(saveData.message || `Failed to save Caddyfile (HTTP ${saveResponse.status})`); }
        
        showToast(`Caddyfile saved. Reloading Caddy...`, "info", 10000);
        const reloadResponse = await fetch('/api/caddy/reload', { method: 'POST' });
        const reloadData = await reloadResponse.json();
        if (!reloadResponse.ok || reloadData.status !== 'success') { 
            let errorMsg = reloadData.message || `Failed to reload Caddy (HTTP ${reloadResponse.status})`; 
            if (reloadData.details) errorMsg += ` Details: ${reloadData.details}`; 
            throw new Error(errorMsg); 
        }
        showToast(`Caddyfile auto-saved and Caddy reloaded!`, "success", 7000);
    } catch (error) {
        console.error('Error during auto save/reload Caddyfile:', error);
        showToast(`Error: ${error.message}`, "danger", 10000);
    } finally {
        isAutoSaving = false;
    }
}

// --- Preferences ---
function applyTheme(themeValue) {
    document.body.className = ''; 
    const effectiveTheme = themeValue || (themeSelect ? themeSelect.value : 'theme-light-gray');
    document.body.classList.add(effectiveTheme);
    localStorage.setItem('caddyPanelTheme', effectiveTheme); 
}

async function savePreferences() {
    if (!themeSelect || !globalAdminEmailInput || !defaultSkipTlsVerifyInput || !defaultAuthentikEnabledInput || !defaultAuthentikOutpostUrlInput || !defaultAuthentikUriInput || !defaultAuthentikCopyHeadersInput || !defaultAuthentikTrustedProxiesInput) { 
        return; 
    }
    const prefsToSave = { 
        theme: themeSelect.value, 
        globalAdminEmail: globalAdminEmailInput.value.trim(), 
        defaultSkipTlsVerify: defaultSkipTlsVerifyInput.checked, 
        defaultAuthentikEnabled: defaultAuthentikEnabledInput.checked, 
        defaultAuthentikOutpostUrl: defaultAuthentikOutpostUrlInput.value.trim(), 
        defaultAuthentikUri: defaultAuthentikUriInput.value.trim(), 
        defaultAuthentikCopyHeaders: defaultAuthentikCopyHeadersInput.value.trim(), 
        defaultAuthentikTrustedProxies: defaultAuthentikTrustedProxiesInput.value.trim() 
    };
    
    if (currentPreferences.caddyfilePath) prefsToSave.caddyfilePath = currentPreferences.caddyfilePath;
    if (currentPreferences.caddyReloadCmd) prefsToSave.caddyReloadCmd = currentPreferences.caddyReloadCmd;
    
    try {
        const response = await fetch('/api/preferences', { 
            method: 'POST', 
            headers: { 'Content-Type': 'application/json' }, 
            body: JSON.stringify(prefsToSave) 
        });
        const result = await response.json();
        if (!response.ok && result.status !== 'warning') { 
            throw new Error(result.message || `HTTP error! status: ${response.status}`); 
        }
        
        currentPreferences = result.saved_prefs || { ...currentPreferences, ...prefsToSave }; 
        
        let message = result.message || 'Preferences saved!';
        let messageType = result.status === 'success' ? 'success' : (result.status === 'warning' ? 'warning' : 'error');
        if (result.errors && result.errors.length > 0) { 
            message += ` Validation errors: ${result.errors.join(', ')}`; 
        }
        showToast(message, messageType);
        applyTheme(currentPreferences.theme);
    } catch (error) {
        console.error('Error saving preferences:', error);
        showToast(`Error saving preferences: ${error.message}`, "danger");
    }
}

async function loadPreferences() {
    try {
        const response = await fetch('/api/preferences');
        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
        currentPreferences = await response.json();

        // Priorité : localStorage > préférences chargées > valeur du select > défaut CSS
        const storedTheme = localStorage.getItem('caddyPanelTheme');
        const themeToApply = storedTheme || currentPreferences.theme || (themeSelect ? themeSelect.value : 'theme-light-gray');
        
        if (themeSelect) themeSelect.value = themeToApply; // Mettre à jour le select
        applyTheme(themeToApply); // Appliquer et stocker
        
        if (globalAdminEmailInput) globalAdminEmailInput.value = currentPreferences.globalAdminEmail || "";
        if (defaultSkipTlsVerifyInput) defaultSkipTlsVerifyInput.checked = currentPreferences.defaultSkipTlsVerify || false;
        if (defaultAuthentikEnabledInput) defaultAuthentikEnabledInput.checked = currentPreferences.defaultAuthentikEnabled || false;
        if (defaultAuthentikOutpostUrlInput) defaultAuthentikOutpostUrlInput.value = currentPreferences.defaultAuthentikOutpostUrl || "";
        if (defaultAuthentikUriInput) defaultAuthentikUriInput.value = currentPreferences.defaultAuthentikUri || "";
        if (defaultAuthentikCopyHeadersInput) defaultAuthentikCopyHeadersInput.value = currentPreferences.defaultAuthentikCopyHeaders || "";
        if (defaultAuthentikTrustedProxiesInput) defaultAuthentikTrustedProxiesInput.value = currentPreferences.defaultAuthentikTrustedProxies || "";

    } catch (error) {
        console.error('Error loading preferences:', error);
        const fallbackTheme = localStorage.getItem('caddyPanelTheme') || (themeSelect ? themeSelect.value : 'theme-light-gray');
        if (themeSelect) themeSelect.value = fallbackTheme;
        applyTheme(fallbackTheme);
        currentPreferences = {}; 
    }
}

// --- Caddyfile Parser (Multi-Site, Table UI) ---
function parseCaddyfile(content) {
    siteConfigs = []; 
    if (globalAdminEmailInput) globalAdminEmailInput.value = '';

    const globalBlockMatch = content.match(/^\{([\s\S]*?)^}/m);
    if (globalBlockMatch && globalBlockMatch[1]) {
        const globalContent = globalBlockMatch[1]; 
        const emailMatch = globalContent.match(/^\s*email\s+([^\s]+)/m);
        if (emailMatch && emailMatch[1] && globalAdminEmailInput) { 
            globalAdminEmailInput.value = emailMatch[1]; 
        }
    }
    
    const siteBlockRegex = /^([a-zA-Z0-9\.\-\*:, ]+?)\s*\{([\s\S]*?)^}/gm;
    const knownDirectives = ['log', 'tls', 'forward_auth', 'try_files', 'handle', 'route', 'header', 'request_header', 'response_header', 'handle_errors', 'encode', 'redir', 'rewrite', 'uri', 'vars', 'import', 'acme_dns', 'auto_https', 'on_demand_tls', 'local_certs', 'skip_log', 'basicauth', 'jwt', 'oidc', 'ip_match', 'remote_ip', 'client_ip', 'method', 'path', 'path_regexp', 'query', 'expression', 'not', 'abort', 'error', 'respond', 'reverse_proxy', 'php_fastcgi', 'file_server', 'root', 'templates', 'markdown', 'push', 'try_files', 'header', 'request_header', 'response_header', 'handle_errors', 'metrics', 'pprof', 'health_check'];
    let match; 
    
    while ((match = siteBlockRegex.exec(content)) !== null) {
        const potentialAddress = match[1].trim(); 
        if (potentialAddress === '' || (match.index === 0 && content.startsWith('{')) || knownDirectives.includes(potentialAddress.toLowerCase().split(/\s+/)[0])) {
            continue; 
        }
        
        const siteAddress = potentialAddress; 
        const siteContent = match[2];
        const siteData = { address: siteAddress, tls: 'auto', tls_skip_verify: false, forward_auth: null };
        
        const rootMatch = siteContent.match(/^\s*root\s+\*\s+([^\s]+)/m); 
        if (rootMatch) siteData.root = rootMatch[1];
        
        if (/^\s*file_server/m.test(siteContent)) siteData.file_server = true;
        
        // START OF BUG FIX
        // Correctly parse reverse_proxy and its 'tls_insecure_skip_verify' option within a block.
        const rpBlockRegex = /^\s*reverse_proxy\s+([^\s{]+)\s*(?:\{([\s\S]*?)\})?/m;
        const rpMatch = siteContent.match(rpBlockRegex);

        // Heuristic to avoid matching a forward_auth directive if it's written as a raw reverse_proxy.
        if (rpMatch && !rpMatch[1].includes('/outpost.goauthentik.io/')) {
            siteData.reverse_proxy = rpMatch[1].trim();
            const blockContent = rpMatch[2] || ''; // Content within {}
            if (blockContent.includes('tls_insecure_skip_verify')) {
                siteData.tls_skip_verify = true;
            }
        }
        // END OF BUG FIX

        const tlsMatch = siteContent.match(/^\s*tls\s+([^\s]+)/m);
        if (tlsMatch) siteData.tls = tlsMatch[1].trim();
        
        const logBlockMatch = siteContent.match(/^\s*log\s*\{([\s\S]*?)\s*}/m);
        if (logBlockMatch) { 
            const logOutputMatch = logBlockMatch[1].match(/^\s*output\s+([^\s]+)/m); 
            if (logOutputMatch) siteData.log = logOutputMatch[1]; 
        }
        
        const forwardAuthBlockRegex = /^\s*forward_auth\s+([^\s]+)\s*\{([\s\S]*?)\s*}/m;
        const forwardAuthMatch = siteContent.match(forwardAuthBlockRegex);
        if (forwardAuthMatch) {
            siteData.forward_auth = { 
                outpost_url: forwardAuthMatch[1].trim(),
                uri: (forwardAuthMatch[2].match(/^\s*uri\s+([^\s]+)/m) || [])[1]?.trim(),
                copy_headers: (forwardAuthMatch[2].match(/^\s*copy_headers\s+(.+)/m) || [])[1]?.trim(),
                trusted_proxies: (forwardAuthMatch[2].match(/^\s*trusted_proxies\s+(.+)/m) || [])[1]?.trim()
            };
        }
        siteConfigs.push(siteData);
    }
    renderSitesTable();
    generateCaddyfileFromData(); 
}

// --- Raw Text Parsing & Import/Export ---
async function handleParseRawTextAndAutoSave() {
    if (!outputTextarea) { return; }
    const rawContent = outputTextarea.value;
    showToast('Parsing raw text...', 'info');
    try {
        parseCaddyfile(rawContent);
        showToast('Parsed successfully! Configurator updated. Saving and reloading...', 'success');
        const configTabButton = document.querySelector('.tab-buttons button[onclick*="configurator-tab"]'); 
        if (configTabButton) { configTabButton.click(); }
        await autoSaveAndReloadCaddy(); 
    } catch (error) {
        console.error("Error during raw text parsing or auto-save:", error);
        showToast(`Error parsing: ${error.message}`, 'danger', 6000);
    }
}

function handleExportCaddyfile() {
    if (!outputTextarea) return;
    generateCaddyfileFromData(); 
    const content = outputTextarea.value;
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const now = new Date();
    const timestamp = `${now.getFullYear()}${(now.getMonth()+1).toString().padStart(2,'0')}${now.getDate().toString().padStart(2,'0')}_${now.getHours().toString().padStart(2,'0')}${now.getMinutes().toString().padStart(2,'0')}`;
    const filename = `Caddyfile_${timestamp}.txt`;
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(link.href);
    showToast('Caddyfile exported successfully!', 'success');
}

async function handleImportCaddyfile(event) {
    const file = event.target.files[0];
    if (!file) { showToast('No file selected.', 'warning'); return; }
    if (!outputTextarea) { showToast('Textarea not found.', 'danger'); return; }
    showToast(`Importing ${file.name}...`, 'info');
    const reader = new FileReader();
    reader.onload = async (e) => {
        try {
            const importedContent = e.target.result;
            outputTextarea.value = importedContent;
            parseCaddyfile(importedContent);
            showToast(`${file.name} imported. Parsing complete. Saving and reloading...`, 'success', 4000);
            await autoSaveAndReloadCaddy();
            if(importCaddyfileInput) importCaddyfileInput.value = '';
        } catch (error) {
            console.error('Error processing imported Caddyfile:', error);
            showToast(`Error importing file: ${error.message}`, 'danger');
        }
    };
    reader.onerror = () => {
        console.error('Error reading file for import.');
        showToast('Error reading file.', 'danger');
    };
    reader.readAsText(file);
}