// Configures where the dashboard looks for the API.
// When running via docker-compose, nginx reverse-proxies /api and /ws to the
// API container — leave AEGIS_API empty. When running the dashboard statically
// and the API on :8000, set this to "http://localhost:8000".
window.AEGIS_API = window.AEGIS_API || "";
