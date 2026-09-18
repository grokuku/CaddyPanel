// Applies the stored theme to auth pages (login/setup).
// Externalized from inline <script> for strict CSP (no 'unsafe-inline').
(function() {
    var theme = localStorage.getItem('caddyPanelTheme');
    if (theme) document.body.className = 'auth-page ' + theme;
})();
