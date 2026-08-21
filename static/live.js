/* Live updates: every dashboard page opens a WebSocket to a /ws/* endpoint;
 * the server re-renders the page's Jinja2 fragment every couple of seconds
 * and pushes the resulting HTML; the client just swaps the region's
 * innerHTML in place. Deliberately not JSON + client-side templating --
 * one rendering implementation (Jinja2, server-side), consistent with the
 * dashboard's "thin JS islands" approach (see docs/DESIGN_DECISIONS.md).
 */

function connectLive(path, regionId, opts) {
    opts = opts || {};
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}${path}`;

    function markConnected(connected) {
        // Fragments carry their own .live-indicator (hidden by default for
        // the very first server-rendered page load); re-mark after every
        // swap, not just on open, or the replacement node reverts to hidden.
        document.querySelectorAll(`#${regionId} .live-indicator`).forEach((el) => {
            el.classList.toggle("connected", connected);
        });
    }

    function connect() {
        const ws = new WebSocket(url);
        ws.onopen = () => markConnected(true);
        ws.onmessage = (event) => {
            const region = document.getElementById(regionId);
            if (!region) return;
            region.innerHTML = event.data;
            markConnected(true);
            if (opts.onUpdate) opts.onUpdate(region);
        };
        ws.onclose = () => {
            // Reconnect with a delay rather than leaving the page silently
            // stale forever (e.g. after the server restarts/redeploys).
            markConnected(false);
            setTimeout(connect, 3000);
        };
        ws.onerror = () => ws.close();
    }
    connect();
}
