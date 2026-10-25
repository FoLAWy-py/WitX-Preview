# Shared by the HTTP and HTTPS server blocks in nginx.conf.
root /usr/share/nginx/html;
client_max_body_size 21m;   # a screenshot upload may be up to 20 MiB

# Every response gets these. No location may have an add_header of its own: one there
# would drop all of these from that location (and the HTTPS block's HSTS), so a location
# sets $cache_control or $csp instead, and an empty value leaves the header out.
set $cache_control "";
set $csp "";
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
add_header Referrer-Policy same-origin always;
add_header Cache-Control $cache_control;
add_header Content-Security-Policy $csp always;

# Text goes out compressed: the app's files as compressed when the image was built
# (gzip_static), the API's JSON as it is sent. Server-sent events are not in gzip_types,
# so each event still leaves at once; screenshots are images and already compressed.
gzip on;
gzip_static on;
gzip_vary on;
gzip_proxied any;
gzip_min_length 1024;
gzip_types application/json application/javascript text/css image/svg+xml;

# Metrics and readiness are for the monitoring system on the internal network.
location = /metrics      { return 404; }
location /health         { return 404; }

# The API (REST, the task event stream, the takeover socket) and the Gateway hub. It
# sends its own Cache-Control, and no CSP: it serves JSON, events and images, never a
# page of the app (its one page, /api/docs, loads Swagger UI from a CDN).
location ~ ^/(api|gateway)/ {
    proxy_pass http://api:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_buffering off;        # server-sent events must not wait in a buffer
    proxy_read_timeout 1h;      # long-lived streams and sockets
    proxy_send_timeout 1h;
}

# The Web app: hashed assets are immutable; every other path is the app's own route.
location /assets/ {
    set $cache_control "public, max-age=31536000, immutable";
    try_files $uri =404;
}
# Only the app's own files run and load: no inline script or style element, nothing
# from another site. Style attributes are allowed: rehype-katex parses KaTeX's HTML,
# full of them, in a <template> of this page (the styles themselves are set from
# script either way). Images: screenshots are blob: URLs, the takeover's live frames
# data: URLs. Fonts: Vite inlines KaTeX's smallest font as a data: URL. connect-src
# 'self' covers the takeover WebSocket in every browser the build targets (Safari 16.4+).
location / {
    set $cache_control "no-cache";
    set $csp "default-src 'none'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' blob: data:; font-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
    try_files $uri /index.html;
}
