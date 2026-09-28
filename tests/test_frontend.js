#!/usr/bin/env node
/*
 * CaddyPanel — front-end regression tests (no browser, no docker, no bundler).
 * ═══════════════════════════════════════════════════════════════════════════
 * These tests protect `static/script.js` and `static/stats.js`, which had NO
 * automated coverage before (the only test suite was Python/pytest). They run
 * the REAL page script inside jsdom — never a frozen copy — against the REAL
 * `<template id="site-modal-template">` extracted from templates/index.html.
 *
 * Coverage:
 *   1. Editing an `is_custom` site shows its stored `custom_content`, keeps it
 *      unchanged on save (idempotent unwrap + normalize), and does not wrap it
 *      twice. (This is the bug fixed in openSiteModal.)
 *   2. Non-regression: a Standard site switched to Custom still regenerates the
 *      content from the standard fields.
 *   3. Guard-rail: classic-script load order of the holaf bricks in the
 *      templates (bricks before the page script; stats.js must not rely on
 *      HolafModal, which stats.html does not load).
 *   4. Guard-rail: NO top-level `import`/`export` in any vendored copy under
 *      static/holaf-*.js (an ESM export in a classic script throws SyntaxError
 *      and breaks the whole page).
 *
 * Usage (single command):
 *   node tests/test_frontend.js
 *
 * Prove it catches regressions (any script path can be substituted):
 *   node tests/test_frontend.js --script /path/to/buggy-script.js
 *
 * jsdom is not a CaddyPanel dependency: the runner resolves it from a locally
 * installed node_modules (e.g. `npm install --no-save jsdom`) or, failing that,
 * from the sibling holaf-lib checkout during local development. See VENDOR.md.
 * Exit codes: 0 = all passed, 1 = a test failed, 3 = jsdom unavailable.
 */
"use strict";

const fs = require("fs");
const path = require("path");

// ── Paths / CLI ─────────────────────────────────────────────────────────────
const ROOT = path.resolve(__dirname, ".."); // CaddyPanel/
const STATIC = path.join(ROOT, "static");
const TEMPLATES = path.join(ROOT, "templates");

function argValue(flag) {
    const i = process.argv.indexOf(flag);
    return i !== -1 && i + 1 < process.argv.length ? process.argv[i + 1] : null;
}
const SCRIPT_OVERRIDE = argValue("--script");
const SCRIPT_PATH = SCRIPT_OVERRIDE ? path.resolve(SCRIPT_OVERRIDE) : path.join(STATIC, "script.js");
const INDEX_HTML = path.join(TEMPLATES, "index.html");
const STATS_HTML = path.join(TEMPLATES, "stats.html");
const STATS_JS = path.join(STATIC, "stats.js");

// ── jsdom resolver (zero new dependency required for local runs) ─────────────
function loadJsdom() {
    const candidates = [];
    if (process.env.CADDYPANEL_JSDOM_PATH) candidates.push(process.env.CADDYPANEL_JSDOM_PATH);
    candidates.push("jsdom"); // installed in CaddyPanel/node_modules (CI or dev)
    const holafLib = process.env.HOLAF_LIB_DIR ? path.resolve(process.env.HOLAF_LIB_DIR) : null;
    const siblings = [
        path.resolve(ROOT, "..", "holaf-lib"), // workspace sibling
        path.resolve(ROOT, "..", "..", "holaf-lib"), // CaddyPanel nested one level deeper
    ];
    if (holafLib) siblings.unshift(holafLib);
    for (const dir of siblings) candidates.push(path.join(dir, "node_modules", "jsdom"));

    // Keep the resolution logic unchanged, but STOP swallowing the real
    // failure: a jsdom that is present but unloadable (e.g. incompatible with
    // the running Node version) used to surface as a misleading "jsdom
    // introuvable". We now collect each attempt's error (type + message) and
    // print it, so the true cause is visible instead of costing hours.
    const errors = [];
    for (const c of candidates) {
        try {
            return require(c);
        } catch (err) {
            const type = (err && err.name) ? err.name : "Error";
            const msg = (err && err.message) ? err.message : String(err);
            errors.push(`   - ${c}\n       ${type}: ${msg}`);
        }
    }
    console.error(
        "⛔ jsdom introuvable ou inchargeable. Tentatives :\n" +
            errors.join("\n") +
            "\n\n" +
            "   Si vous lisez « SyntaxError » ou une erreur de version, c'est que\n" +
            "   jsdom EST installe mais INCOMPATIBLE : jsdom@30 exige\n" +
            "   node ^22.22.2 || ^24.15.0 || >=26 (voir `node --version`).\n" +
            "   Installer puis relancer :\n" +
            "     npm install --no-save jsdom\n" +
            "   ou pointer vers une install existante :\n" +
            "     CADDYPANEL_JSDOM_PATH=/chemin/node_modules/jsdom node tests/test_frontend.js"
    );
    process.exit(3);
}
const { JSDOM } = loadJsdom();

// ── Tiny assertion harness ─────────────────────────────────────────────────
let failures = 0;
let passed = 0;
function check(label, cond, extra) {
    if (cond) {
        passed++;
        console.log("PASS  " + label);
    } else {
        failures++;
        console.log("FAIL  " + label + (extra !== undefined ? " :: " + extra : ""));
    }
}
function section(title) {
    console.log("\n— " + title + " —");
}

// ═══════════════════════════════════════════════════════════════════════════
// Guard-rail 4 — no top-level import/export in any vendored copy
// ═══════════════════════════════════════════════════════════════════════════
function checkVendoredNoEsm() {
    section("Vendored copies: no top-level ESM");
    const vendored = fs
        .readdirSync(STATIC)
        .filter((n) => /^holaf-.*\.js$/.test(n))
        .sort();
    check("at least the 4 expected vendored bricks are present", vendored.length >= 4, vendored.join(", "));
    for (const name of vendored) {
        const src = fs.readFileSync(path.join(STATIC, name), "utf8");
        // Classic scripts must not carry top-level import/export (column 0).
        const offenders = src
            .split("\n")
            .map((line, i) => [i + 1, line])
            .filter(([, line]) => /^(import|export)[\s{]/.test(line));
        check(
            `static/${name}: no top-level import/export`,
            offenders.length === 0,
            offenders.map(([n, l]) => `L${n}: ${l.trim()}`).join(" | ")
        );
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// Guard-rail 3 — script load order in the templates
// ═══════════════════════════════════════════════════════════════════════════
function extractScriptFiles(html) {
    // Jinja: src="{{ url_for('static', filename='X') }}" → capture X in order.
    const files = [];
    const re = /<script\b[^>]*\bsrc="([^"]+)"/g;
    let m;
    while ((m = re.exec(html)) !== null) {
        const fm = /filename='([^']+)'/.exec(m[1]);
        if (fm) files.push(fm[1]);
    }
    return files;
}

function checkScriptOrder() {
    section("Templates: brick load order");
    const expectedBricks = ["holaf-icons.js", "holaf-fetch.js", "holaf-toast.js"];

    const index = extractScriptFiles(fs.readFileSync(INDEX_HTML, "utf8"));
    const stats = extractScriptFiles(fs.readFileSync(STATS_HTML, "utf8"));

    for (const [label, files, consumer] of [
        ["templates/index.html", index, "script.js"],
        ["templates/stats.html", stats, "stats.js"],
    ]) {
        const consumerIdx = files.indexOf(consumer);
        check(`${label}: loads ${consumer}`, consumerIdx !== -1, files.join(", "));
        for (const brick of expectedBricks) {
            const i = files.indexOf(brick);
            check(`${label}: loads ${brick}`, i !== -1, files.join(", "));
            check(
                `${label}: ${brick} loaded before ${consumer}`,
                i !== -1 && consumerIdx !== -1 && i < consumerIdx,
                files.join(", ")
            );
        }
    }

    // index.html uses HolafModal (script.js opens modals) → it must be loaded.
    check("templates/index.html: loads holaf-modal.js", index.includes("holaf-modal.js"), index.join(", "));
    // stats.html must NOT load HolafModal (it is not used on that page).
    check("templates/stats.html: does NOT load holaf-modal.js", !stats.includes("holaf-modal.js"), stats.join(", "));
    // ...and stats.js must genuinely not reference HolafModal.
    const statsJs = fs.readFileSync(STATS_JS, "utf8");
    check("static/stats.js: does not reference HolafModal", !/HolafModal/.test(statsJs));
}

// ═══════════════════════════════════════════════════════════════════════════
// Functional tests — run the REAL script.js in jsdom
// ═══════════════════════════════════════════════════════════════════════════
function extractTemplateBlock(html, id) {
    const startTag = `<template id="${id}">`;
    const start = html.indexOf(startTag);
    if (start === -1) throw new Error(`<template id="${id}"> not found in templates/index.html`);
    const end = html.indexOf("</template>", start) + "</template>".length;
    return html.slice(start, end);
}

function buildWindow() {
    const scriptSrc = fs.readFileSync(SCRIPT_PATH, "utf8");
    const indexHtml = fs.readFileSync(INDEX_HTML, "utf8");
    const templateBlock = extractTemplateBlock(indexHtml, "site-modal-template");

    const dom = new JSDOM(
        `<!doctype html><html><head><meta name="csrf-token" content="test-csrf"></head>` +
            `<body class="theme-light-gray">${templateBlock}` +
            `<textarea id="caddyfile-output"></textarea>` +
            `<tbody id="sites-table-body"></tbody>` +
            `</body></html>`,
        { runScripts: "outside-only", pretendToBeVisual: true, url: "http://localhost/" }
    );
    const { window } = dom;

    // Stub the holaf-lib bricks + the network. HolafModal.open mimics the real
    // brick: it appends the content node to the document on open.
    window.HolafModal = {
        open: (opts) => {
            if (opts && opts.content) window.document.body.appendChild(opts.content);
            return {
                close: () => {
                    if (opts && opts.content && opts.content.remove) opts.content.remove();
                    if (opts && typeof opts.onClose === "function") opts.onClose();
                },
            };
        },
    };
    window.HolafToast = { show: () => {} };
    window.HolafIcons = { get: () => "<svg></svg>", render: () => "<svg></svg>" };
    window.HolafFetch = { request: async () => ({ status: "success", message: "ok" }) };
    window.fetch = async () => ({ json: async () => ({}) });

    // Load the real page script and expose the internals under test.
    window.eval(
        scriptSrc +
            `\n;globalThis.__CaddyTest = {` +
            `  openSiteModal, toggleModalMode, saveSiteFromModal,` +
            `  normalizeCustomIndentation, unwrapSiteBlockIfNeeded,` +
            `  setState: (sc, cp) => { siteConfigs = sc; currentPreferences = cp; },` +
            `  getSiteConfigs: () => siteConfigs` +
            `};`
    );

    const api = window.__CaddyTest;
    if (!api) throw new Error("script.js did not expose __CaddyTest (load error?)");
    return { window, document: window.document, api };
}

async function runFunctionalTests() {
    section(`Functional: real script (${path.relative(ROOT, SCRIPT_PATH)}) in jsdom`);
    const { document, api } = buildWindow();

    // ===== Scenario A: edit an is_custom site (the reported bug) =====
    const stored = "reverse_proxy http://localhost:9000\n\theader X-Custom yes";
    api.setState([{ address: "custom.example.com", is_custom: true, custom_content: stored }], {});
    api.openSiteModal(0);

    const ta = document.getElementById("modal-custom-content");
    check("A1 textarea shows the stored custom_content (not empty)", ta.value === stored, JSON.stringify(ta.value));
    check(
        "A2 custom mode visible, standard hidden",
        document.getElementById("modal-custom-config").style.display === "block" &&
            document.getElementById("modal-standard-config").style.display === "none"
    );
    check("A3 modal-site-index is set to the edited index", document.getElementById("modal-site-index").value === "0");

    // unwrap+normalize must be idempotent: a save without modification must not
    // alter the stored content (no double normalize / re-wrapping).
    const reNormalized = api.normalizeCustomIndentation(api.unwrapSiteBlockIfNeeded(ta.value));
    check("A4 unwrap+normalize is idempotent", reNormalized === stored, JSON.stringify(reNormalized));

    await api.saveSiteFromModal();
    const saved = api.getSiteConfigs()[0];
    check(
        "A5 save without modification keeps custom_content identical",
        !!saved && saved.is_custom === true && saved.custom_content === stored,
        JSON.stringify(saved && saved.custom_content)
    );

    // ===== Scenario B: Standard -> Custom still regenerates (no regression) =====
    api.setState([{ address: "std.example.com", reverse_proxy: "localhost:8080", tls: "auto", is_custom: false }], {});
    api.openSiteModal(0);
    const ta2 = document.getElementById("modal-custom-content");
    check(
        "B1 standard site opens in standard mode (textarea empty)",
        document.getElementById("modal-custom-config").style.display === "none" && ta2.value === ""
    );
    api.toggleModalMode("custom"); // user clicks the "Custom Config" button
    check(
        "B2 clicking Custom Config still generates content from standard fields",
        ta2.value.includes("reverse_proxy http://localhost:8080"),
        JSON.stringify(ta2.value)
    );
}

// ═══════════════════════════════════════════════════════════════════════════
(async () => {
    console.log(`CaddyPanel front-end tests — root: ${ROOT}`);
    checkScriptOrder();
    checkVendoredNoEsm();
    try {
        await runFunctionalTests();
    } catch (err) {
        failures++;
        console.log("FAIL  functional suite crashed :: " + (err && err.message));
    }

    console.log(`\n${passed} passed, ${failures} failed`);
    console.log(failures === 0 ? "ALL TESTS PASSED" : failures + " TEST(S) FAILED");
    process.exit(failures === 0 ? 0 : 1);
})();
