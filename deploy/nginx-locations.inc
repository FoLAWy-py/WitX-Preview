# Shared by the HTTP and HTTPS server blocks in nginx.conf.
root /usr/share/nginx/html;
client_max_body_size 21m;   # a screenshot upload may be up to 20 MiB

add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
add_header Referrer-Policy same-origin always;

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

# The API (REST, the task event stream, the takeover socket) and the Gateway hub.
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
    add_header Cache-Control "public, max-age=31536000, immutable";
    add_header X-Content-Type-Options nosniff always;
    try_files $uri =404;
}
location / {
    add_header Cache-Control "no-cache";
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy same-origin always;
    try_files $uri /index.html;
}
