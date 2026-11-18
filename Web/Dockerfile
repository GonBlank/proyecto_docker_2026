FROM nginx:1.28-alpine

# Configuración del sitio.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Página y librería de gráficos.
COPY index.html echarts.min.js /usr/share/nginx/html/

EXPOSE 80
