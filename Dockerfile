FROM python:3.12-slim
WORKDIR /app
COPY . .
# Oeffentliche DEMO: nur Testdaten! Fuer echte Daten CHEF_DEMO weglassen und CHEF_ADMIN_PIN setzen.
ENV CHEF_DEMO=1 CHEF_HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["python", "chef.py", "serve"]
