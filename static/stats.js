// Statistics page script (externalized from templates/stats.html for CSP hardening).
// Reads backend URLs from the #stats-config JSON script (type="application/json",
// which is not executed and therefore allowed by a strict CSP).

const statsConfig = JSON.parse(document.getElementById('stats-config').textContent);

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function getCsrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || '';
}

// --- API helper (HolafFetch + CaddyPanel contract) ---
// HolafFetch throws on ANY non-2xx status and builds its message from
// body.error/body.detail; the CaddyPanel API returns {status, message,
// details}. This adapter restores the (ok, payload) shape used here so server
// messages/details keep reaching the toasts. Transport-level failures are
// re-thrown for the existing catch blocks.
async function apiFetch(url, opts = {}) {
    try {
        const payload = await HolafFetch.request(url, opts);
        return { ok: true, status: 200, payload };
    } catch (err) {
        if (err && err.name === 'HolafFetchError' && err.status && err.data) {
            return { ok: false, status: err.status, payload: err.data };
        }
        throw err;
    }
}

function apiErrorMessage(payload, fallback) {
    let msg = (payload && payload.message) || fallback;
    if (payload && payload.details) msg += ` Details: ${payload.details}`;
    return msg;
}

let currentPeriod = '7d';
let currentHost = null;
const urlParams = new URLSearchParams(window.location.search);
if (urlParams.has('period')) currentPeriod = urlParams.get('period');
if (urlParams.has('host'))   currentHost  = urlParams.get('host') || null;

// Country code → flag.
// The flag is INTENTIONALLY an emoji (regional-indicator pair) — NOT a
// migration oversight: it renders on the target Linux host and scales with
// the surrounding font. The HolafIcons "globe" SVG is only the fallback for an
// unknown/invalid code; its size/alignment is handled by the `.country-flag svg`
// rule in templates/stats.html so it matches the emoji.
function countryFlag(code) {
    if (!code || code === 'Unknown' || code.length !== 2) return HolafIcons.get('globe');
    const c1 = code.charCodeAt(0) - 65, c2 = code.charCodeAt(1) - 65;
    if (c1 < 0 || c1 > 25 || c2 < 0 || c2 > 25) return HolafIcons.get('globe');
    return String.fromCodePoint(0x1F1E6 + c1, 0x1F1E6 + c2);
}

const COUNTRY_NAMES = {
    AF:'Afghanistan',AL:'Albania',DZ:'Algeria',AD:'Andorra',AO:'Angola',AG:'Antigua',AR:'Argentina',AM:'Armenia',
    AU:'Australia',AT:'Austria',AZ:'Azerbaijan',BS:'Bahamas',BH:'Bahrain',BD:'Bangladesh',BY:'Belarus',BE:'Belgium',
    BZ:'Belize',BJ:'Benin',BO:'Bolivia',BA:'Bosnia',BW:'Botswana',BR:'Brazil',BN:'Brunei',BG:'Bulgaria',
    BF:'Burkina Faso',BI:'Burundi',KH:'Cambodia',CM:'Cameroon',CA:'Canada',CV:'Cape Verde',CF:'Cent. Africa',
    TD:'Chad',CL:'Chile',CN:'China',CO:'Colombia',CD:'Congo (DRC)',CR:'Costa Rica',HR:'Croatia',CU:'Cuba',
    CY:'Cyprus',CZ:'Czechia',DK:'Denmark',DJ:'Djibouti',DO:'Dominican Rep.',EC:'Ecuador',EG:'Egypt',SV:'El Salvador',
    EE:'Estonia',ET:'Ethiopia',FI:'Finland',FR:'France',GA:'Gabon',GE:'Georgia',DE:'Germany',GH:'Ghana',
    GR:'Greece',GT:'Guatemala',GN:'Guinea',HT:'Haiti',HN:'Honduras',HU:'Hungary',IS:'Iceland',IN:'India',
    ID:'Indonesia',IR:'Iran',IQ:'Iraq',IE:'Ireland',IL:'Israel',IT:'Italy',JM:'Jamaica',JP:'Japan',
    JO:'Jordan',KZ:'Kazakhstan',KE:'Kenya',KR:'Korea (South)',KW:'Kuwait',LV:'Latvia',LB:'Lebanon',
    LY:'Libya',LT:'Lithuania',LU:'Luxembourg',MG:'Madagascar',MY:'Malaysia',ML:'Mali',MT:'Malta',
    MX:'Mexico',MD:'Moldova',MA:'Morocco',MZ:'Mozambique',MM:'Myanmar',NA:'Namibia',NP:'Nepal',
    NL:'Netherlands',NZ:'New Zealand',NI:'Nicaragua',NE:'Niger',NG:'Nigeria',MK:'North Macedonia',
    NO:'Norway',OM:'Oman',PK:'Pakistan',PA:'Panama',PY:'Paraguay',PE:'Peru',PH:'Philippines',PL:'Poland',
    PT:'Portugal',QA:'Qatar',RO:'Romania',RU:'Russia',RW:'Rwanda',SA:'Saudi Arabia',SN:'Senegal',
    RS:'Serbia',SG:'Singapore',SK:'Slovakia',SI:'Slovenia',SO:'Somalia',ZA:'South Africa',ES:'Spain',
    LK:'Sri Lanka',SE:'Sweden',CH:'Switzerland',TW:'Taiwan',TJ:'Tajikistan',
    TZ:'Tanzania',TH:'Thailand',TN:'Tunisia',TR:'Turkey',TM:'Turkmenistan',UG:'Uganda',UA:'Ukraine',
    AE:'UAE',GB:'United Kingdom',US:'United States',UY:'Uruguay',UZ:'Uzbekistan',VE:'Venezuela',
    VN:'Vietnam',YE:'Yemen',ZM:'Zambia',ZW:'Zimbabwe'
};
function countryName(code) { return COUNTRY_NAMES[code] || code; }

function showToast(message, type = 'info', duration = 5000) {
    // "danger" (CaddyPanel legacy) → "error" (HolafToast type).
    HolafToast.show({ message, type: type === 'danger' ? 'error' : type, duration });
}

function applyStoredTheme() {
    const s = localStorage.getItem('caddyPanelTheme');
    document.body.className = '';
    document.body.classList.add(s || 'theme-light-gray');
}

async function loadHosts() {
    try {
        const resp = await apiFetch(statsConfig.get_stats_hosts_url, { method: 'GET' });
        if (!resp.ok) return;
        const hosts = resp.payload;
        const sel = document.getElementById('host-select');
        hosts.forEach(h => {
            if (!sel.querySelector(`option[value="${CSS.escape(h)}"]`)) {
                const opt = document.createElement('option');
                opt.value = h; opt.textContent = h; sel.appendChild(opt);
            }
        });
        if (currentHost && !sel.querySelector(`option[value="${CSS.escape(currentHost)}"]`)) {
            const opt = document.createElement('option');
            opt.value = currentHost; opt.textContent = currentHost; sel.appendChild(opt);
        }
        sel.value = currentHost || '';
    } catch (e) { console.error('Error loading hosts:', e); }
}

function setActivePeriod(period) {
    currentPeriod = period;
    document.querySelectorAll('.period-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.period === period));
}

function updateChartTitle(period) {
    document.getElementById('chart-title').textContent = (period === '24h' || period === '7d') ? 'Requests per Hour' : 'Requests per Day';
}

function updatePageTitle(host) {
    const h1 = document.getElementById('page-title');
    h1.textContent = host ? `Stats — ${host}` : 'Global Statistics';
    document.title = host ? `Stats - ${host} - CaddyPanel` : 'Statistics - CaddyPanel';
}

function populateList(ulId, data, defaultText = 'No data.') {
    const ul = document.getElementById(ulId);
    ul.innerHTML = '';
    if (data && (Array.isArray(data) ? data.length : Object.keys(data).length) > 0) {
        const items = Array.isArray(data) ? data : Object.entries(data);
        items.forEach(item => {
            const li = document.createElement('li');
            let label, value;
            if (Array.isArray(item)) { label = item[0]; value = item[1]; }
            else { label = item.path || item.agent || 'Unknown'; value = item.count; }
            const safeLabel = escapeHtml(label);
            li.innerHTML = `<span class="label" title="${safeLabel}">${safeLabel}</span> <span class="value">${value.toLocaleString()}</span>`;
            ul.appendChild(li);
        });
    } else {
        ul.innerHTML = `<li class="label">${defaultText}</li>`;
    }
}

function renderStatusBar(dist) {
    const bar = document.getElementById('status-bar');
    const legend = document.getElementById('status-legend');
    bar.innerHTML = ''; legend.innerHTML = '';
    const total = Object.values(dist).reduce((a,b) => a+b, 0);
    if (!total) return;
    const codes = ['1xx','2xx','3xx','4xx','5xx'];
    codes.forEach(code => {
        const count = dist[code] || 0;
        const pct = count / total * 100;
        if (count > 0) {
            const seg = document.createElement('div');
            seg.className = `status-segment status-${code}`;
            seg.style.width = `${pct}%`;
            bar.appendChild(seg);
        }
        const item = document.createElement('span');
        item.className = 'status-legend-item';
        item.innerHTML = `<span class="status-dot status-${escapeHtml(code)}"></span>${escapeHtml(code)}: ${count.toLocaleString()} (${pct.toFixed(1)}%)`;
        legend.appendChild(item);
    });
}

function renderCountries(countries) {
    const ul = document.getElementById('top-countries-list');
    ul.innerHTML = '';
    if (!countries || countries.length === 0) {
        ul.innerHTML = '<li class="geoip-unavailable">No country data available</li>';
        return;
    }
    const maxCount = countries[0]?.count || 1;
    countries.slice(0, 5).forEach(c => {
        const li = document.createElement('li');
        const pct = (c.count / maxCount * 100).toFixed(0);
        li.innerHTML = `
            <span class="country-label">
                <span class="country-flag">${countryFlag(c.country)}</span>
                ${escapeHtml(countryName(c.country))}
            </span>
            <span style="display:flex;align-items:center;">
                <span class="country-value">${c.count.toLocaleString()}</span>
                <span class="country-bar-bg"><span class="country-bar" style="width:${pct}%"></span></span>
            </span>`;
        ul.appendChild(li);
    });
}

// --- World map (D3 + TopoJSON) ---
async function renderWorldMap(countryData) {
    const mapDiv = document.getElementById('world-map-container');
    mapDiv.innerHTML = '';
    if (!countryData || countryData.length === 0) {
        mapDiv.innerHTML = '<div class="map-no-data">No geographic data</div>';
        return;
    }
    try {
        const world = await d3.json(statsConfig.world_atlas_url);
        const countries = topojson.feature(world, world.objects.countries);
        const borders = topojson.mesh(world, world.objects.countries, (a, b) => a !== b);

        const A2_TO_NUMERIC = {
            AF:'004',AL:'008',DZ:'012',AD:'020',AO:'024',AG:'028',AR:'032',AM:'051',AU:'036',AT:'040',AZ:'031',
            BS:'044',BH:'048',BD:'050',BY:'112',BE:'056',BZ:'084',BJ:'204',BO:'068',BA:'070',BW:'072',BR:'076',
            BN:'096',BG:'100',BF:'854',BI:'108',KH:'116',CM:'120',CA:'124',CV:'132',CF:'140',TD:'148',CL:'152',
            CN:'156',CO:'170',CD:'180',CR:'188',HR:'191',CU:'192',CY:'196',CZ:'203',DK:'208',DJ:'262',DO:'214',
            EC:'218',EG:'818',SV:'222',EE:'233',ET:'231',FI:'246',FR:'250',GA:'266',GE:'268',DE:'276',GH:'288',
            GR:'300',GT:'320',GN:'324',HT:'332',HN:'340',HU:'348',IS:'352',IN:'356',ID:'360',IR:'364',IQ:'368',
            IE:'372',IL:'376',IT:'380',JM:'388',JP:'392',JO:'400',KZ:'398',KE:'404',KR:'410',KW:'414',LV:'428',
            LB:'422',LY:'434',LT:'440',LU:'442',MG:'450',MY:'458',ML:'466',MT:'470',MX:'484',MD:'498',MA:'504',
            MZ:'508',MM:'104',NA:'516',NP:'524',NL:'528',NZ:'554',NI:'558',NE:'562',NG:'566',MK:'807',NO:'578',
            OM:'512',PK:'586',PA:'591',PY:'600',PE:'604',PH:'608',PL:'616',PT:'620',QA:'634',RO:'642',RU:'643',
            RW:'646',SA:'682',SN:'686',RS:'688',SG:'702',SK:'703',SI:'705',SO:'706',ZA:'710',ES:'724',LK:'144',
            SD:'729',SE:'752',CH:'756',SY:'760',TW:'158',TJ:'762',TZ:'834',TH:'764',TN:'788',TR:'792',TM:'795',
            UG:'800',UA:'804',AE:'784',GB:'826',US:'840',UY:'858',UZ:'860',VE:'862',VN:'704',YE:'887',ZM:'894',
            ZW:'716',PS:'275'
        };
        const maxCount = countryData[0]?.count || 1;
        const countByNumeric = {};
        countryData.forEach(c => {
            const num = A2_TO_NUMERIC[c.country];
            if (num) countByNumeric[num] = c.count;
        });

        const isDark = Array.from(document.body.classList).some(c => c.startsWith('theme-dark-'));
        const landColor = isDark ? '#374151' : '#e5e7eb';
        const borderCol = isDark ? '#4b5563' : '#f9fafb';
        const oceanCol  = isDark ? '#111827' : '#f3f4f6';

        const width = mapDiv.clientWidth || 300;
        const height = mapDiv.clientHeight || 150;

        const projection = d3.geoNaturalEarth1().fitSize([width, height], countries).precision(0.1);
        const path = d3.geoPath(projection);
        const tooltip = document.getElementById('map-tooltip');

        // Accent color for scale
        const accentStyle = getComputedStyle(document.body);
        const accentColor = accentStyle.getPropertyValue('--accent').trim() || '#3b82f6';

        const svg = d3.select(mapDiv).append('svg')
            .attr('width', width).attr('height', height)
            .style('display', 'block')
            .style('max-height', '100%');

        svg.append('rect').attr('width', width).attr('height', height).attr('fill', oceanCol);

        const colorScale = d3.scaleSequential(d3.interpolateYlOrRd).domain([0, maxCount || 1]);

        svg.selectAll('path.country')
            .data(countries.features)
            .enter().append('path')
            .attr('class', 'country')
            .attr('d', path)
            .attr('fill', d => countByNumeric[d.id] ? colorScale(countByNumeric[d.id]) : landColor)
            .attr('stroke', borderCol)
            .attr('stroke-width', 0.3)
            .on('mouseover', function(event, d) {
                const cnt = countByNumeric[d.id];
                if (cnt) {
                    d3.select(this).attr('stroke', isDark ? '#9ca3af' : '#374151').attr('stroke-width', 1);
                    tooltip.textContent = `${d.properties?.name || d.id}: ${cnt.toLocaleString()} req`;
                    tooltip.style.opacity = 1;
                }
            })
            .on('mousemove', function(event) {
                tooltip.style.left = (event.pageX + 12) + 'px';
                tooltip.style.top = (event.pageY - 20) + 'px';
            })
            .on('mouseout', function() {
                d3.select(this).attr('stroke', borderCol).attr('stroke-width', 0.3);
                tooltip.style.opacity = 0;
            });

        svg.append('path')
            .datum(borders)
            .attr('fill', 'none')
            .attr('stroke', borderCol)
            .attr('stroke-width', 0.4)
            .attr('d', path);

    } catch (e) {
        console.error('World map render error:', e);
        mapDiv.innerHTML = '<div class="map-no-data">Map unavailable</div>';
    }
}

function renderChart(canvas, timeseries, period) {
    let existing = Chart.getChart(canvas);
    if (existing) existing.destroy();
    if (!timeseries || timeseries.length === 0) { canvas.style.display = 'none'; return; }
    canvas.style.display = 'block';

    const isDark = Array.from(document.body.classList).some(c => c.startsWith('theme-dark-'));
    const tickC   = isDark ? '#9ca3af' : '#6b7280';
    const legendC = isDark ? '#e5e7eb' : '#374151';
    const gridC   = isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.07)';
    const accent  = getComputedStyle(document.body).getPropertyValue('--accent').trim() || '#3b82f6';
    const bgFill  = isDark ? 'rgba(96,165,250,0.15)' : 'rgba(59,130,246,0.12)';

    new Chart(canvas.getContext('2d'), {
        type: 'line',
        data: {
            labels: timeseries.map(d => d.time),
            datasets: [{
                label: 'Requests',
                data: timeseries.map(d => d.count),
                borderColor: accent,
                backgroundColor: bgFill,
                tension: 0.1, fill: true,
                pointRadius: timeseries.length > 100 ? 0 : 2,
            }]
        },
        options: {
            scales: {
                x: {
                    title: { display: true, text: (period === '24h' || period === '7d') ? 'Time (UTC)' : 'Date (UTC)', color: legendC },
                    ticks: { color: tickC, maxTicksLimit: 12, autoSkip: true },
                    grid: { color: gridC },
                },
                y: {
                    beginAtZero: true,
                    title: { display: true, text: 'Requests', color: legendC },
                    ticks: { color: tickC },
                    grid: { color: gridC },
                }
            },
            plugins: { legend: { labels: { color: legendC } } },
            responsive: true, maintainAspectRatio: false,
        }
    });
}

async function fetchAndDisplayStats() {
    const loadingEl  = document.getElementById('loading-message');
    const contentEl  = document.getElementById('stats-content');
    const periodEl   = document.getElementById('data-period-display');
    const logArea    = document.getElementById('log-config-action-area');
    const logMsg     = document.getElementById('log-config-message');

    loadingEl.textContent = 'Loading statistics...';
    loadingEl.style.color = '';
    loadingEl.style.display = 'block';
    logArea.style.display = 'none';
    contentEl.style.display = 'none';

    updatePageTitle(currentHost);
    updateChartTitle(currentPeriod);

    let apiUrl = statsConfig.get_stats_url;
    const params = new URLSearchParams();
    params.set('period', currentPeriod);
    if (currentHost) params.set('host', currentHost);
    apiUrl += '?' + params.toString();

    try {
        const resp = await apiFetch(apiUrl, { method: 'GET' });
        if (!resp.ok) throw new Error(apiErrorMessage(resp.payload, `HTTP ${resp.status}`));
        const stats = resp.payload;

        if (stats.log_read_error) {
            logMsg.innerHTML = `<strong>Warning:</strong> ${escapeHtml(stats.log_read_error)}`;
            logArea.style.display = 'block';
            loadingEl.style.display = 'none';
        } else {
            loadingEl.style.display = 'none';
            logArea.style.display = 'none';
        }
        contentEl.style.display = 'grid';

        document.getElementById('total-requests').textContent = (stats.total_requests || 0).toLocaleString();
        document.getElementById('error-rate').textContent = (stats.error_rate_percent || 0).toFixed(1) + '%';
        document.getElementById('avg-response-time').textContent = (stats.avg_response_time_ms || 0).toFixed(0) + ' ms';
        document.getElementById('avg-response-size').textContent = (stats.avg_response_size_kb || 0).toFixed(1) + ' KB';

        if (stats.data_from_utc && stats.data_from_utc !== 'N/A')
            periodEl.textContent = `${stats.data_from_utc} → ${stats.data_to_utc}`;
        else periodEl.textContent = '';

        renderStatusBar(stats.status_codes_dist || {});
        populateList('requests-by-host', stats.requests_by_host, 'No host data.');
        populateList('top-paths', stats.top_paths, 'No path data.');
        populateList('top-user-agents', stats.top_user_agents, 'No UA data.');
        renderCountries(stats.top_countries || []);
        renderWorldMap(stats.top_countries || []);

        const canvas = document.getElementById('requests-timeseries-chart');
        renderChart(canvas, stats.requests_timeseries, currentPeriod);

    } catch (err) {
        console.error('Error fetching stats:', err);
        loadingEl.textContent = `Error: ${err.message}`;
        loadingEl.style.color = '#ef4444';
        loadingEl.style.display = 'block';
        contentEl.style.display = 'none';
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    applyStoredTheme();
    setActivePeriod(currentPeriod);
    await loadHosts();
    await fetchAndDisplayStats();

    document.querySelectorAll('.period-btn').forEach(btn => {
        btn.addEventListener('click', () => { setActivePeriod(btn.dataset.period); fetchAndDisplayStats(); });
    });
    document.getElementById('host-select').addEventListener('change', (e) => { currentHost = e.target.value || null; fetchAndDisplayStats(); });

    const cfgBtn = document.getElementById('configure-logging-btn');
    if (cfgBtn) {
        cfgBtn.addEventListener('click', async () => {
            showToast('Configuring Caddyfile for logging...', 'info', 10000);
            cfgBtn.disabled = true; cfgBtn.textContent = 'Configuring...';
            try {
                const resp = await apiFetch(statsConfig.configure_caddyfile_logging_url, { method: 'POST', headers: { 'X-CSRFToken': getCsrfToken() } });
                const result = resp.payload || {};
                if (resp.ok && result.status === 'success') {
                    showToast(result.message, 'success', 7000);
                    document.getElementById('log-config-action-area').style.display = 'none';
                    setTimeout(fetchAndDisplayStats, 2000);
                } else {
                    showToast(apiErrorMessage(result, `Error: ${resp.status}`),
                              result.status === 'warning' ? 'warning' : 'danger', 10000);
                }
            } catch (err) { showToast(`Error: ${err.message}`, 'danger', 10000); }
            finally { cfgBtn.disabled = false; cfgBtn.textContent = 'Configure Caddy Logging'; }
        });
    }
});
