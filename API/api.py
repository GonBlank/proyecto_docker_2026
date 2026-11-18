"""API de la página: administra los hosts y devuelve sus mediciones.
nginx (servicio web) le reenvía los pedidos que empiezan con /api/.
Las consultas SNMP las hace el worker (servicio python)."""
import ipaddress
import os
from datetime import datetime, timezone

import docker
import pymysql
from flask import Flask, jsonify, request

app = Flask(__name__)
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', '30'))


def connect_db():
    # DB_HOST vale "db": el nombre del servicio de MariaDB en docker-compose.yml.
    return pymysql.connect(
        host=os.environ['DB_HOST'],
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        database=os.environ['DB_NAME'],
        cursorclass=pymysql.cursors.DictCursor,  # filas como diccionarios
        autocommit=True,                         # guarda cada cambio al instante
    )


# Si la base rechaza un dato, se responde con un mensaje entendible.
@app.errorhandler(pymysql.IntegrityError)
def duplicated(error):
    return jsonify(error='Ya existe un host con esa IP y puerto.'), 409


@app.errorhandler(pymysql.MySQLError)
def database_error(error):
    return jsonify(error='No se pudo usar la base de datos. Revisá el servicio db.'), 503


@app.get('/api/health')
def health():
    # Prueba que la API funcione y que llegue a la base.
    with connect_db() as db, db.cursor() as cursor:
        cursor.execute('SELECT 1')
    return jsonify(status='ok', poll_interval=POLL_INTERVAL)


@app.get('/api/app-status')
def app_status():
    # Pregunta a Docker (como "docker ps -a") por los contenedores de este proyecto.
    try:
        client = docker.from_env()
        project = os.environ['DOCKER_PROJECT_NAME']
        containers = client.containers.list(
            all=True, filters={'label': f'com.docker.compose.project={project}'})
    except Exception:
        return jsonify(error='No se pudo consultar Docker.'), 503

    by_service = {c.labels['com.docker.compose.service']: c for c in containers}
    services = []
    for service in ('db', 'python', 'api', 'web'):
        container = by_service.get(service)
        if container is None:
            services.append({'service': service, 'status': 'missing'})
            continue
        state = container.attrs['State']
        services.append({
            'service': service,
            'status': state['Status'],                          # running, exited...
            'health': state.get('Health', {}).get('Status'),    # sólo db tiene healthcheck
            'name': container.name,
            'image': container.attrs['Config']['Image'],
            'started_at': state['StartedAt'],
            'restart_count': container.attrs['RestartCount'],
        })
    return jsonify(services=services, checked_at=datetime.now(timezone.utc).isoformat())


@app.get('/api/hosts')
def list_hosts():
    with connect_db() as db, db.cursor() as cursor:
        # Cada host junto con su última medición (de la configuración actual).
        cursor.execute('''
            SELECT h.id, h.name, h.ip, h.port, h.enabled,
                   s.checked_at, s.status, s.response_ms, s.sys_name, s.sys_descr,
                   s.uptime_seconds, s.cpu_percent, s.memory_percent,
                   s.memory_total_mb, s.error
            FROM hosts h LEFT JOIN samples s ON s.id = (
                SELECT id FROM samples WHERE host_id = h.id
                AND config_version = h.config_version ORDER BY id DESC LIMIT 1
            ) ORDER BY h.name
        ''')
        hosts = cursor.fetchall()

    # Estados que no salen de SNMP: pausado, pendiente o desactualizado.
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # la base guarda horas UTC
    for host in hosts:
        host['enabled'] = bool(host['enabled'])
        if not host['enabled']:
            host['status'] = 'paused'
        elif host['checked_at'] is None:
            host['status'] = 'pending'
        elif (now - host['checked_at']).total_seconds() > max(90, POLL_INTERVAL * 3):
            host['status'] = 'stale'
    return jsonify(hosts=hosts, poll_interval=POLL_INTERVAL)


def check_host(data, editing):
    # Devuelve un mensaje de error, o None si los datos están bien.
    if not data.get('name', '').strip():
        return 'Ingresá un nombre.'
    try:
        ipaddress.IPv4Address(data.get('ip', ''))
    except ValueError:
        return 'Ingresá una dirección IPv4 válida, por ejemplo 192.168.1.20.'
    if not 1 <= data.get('port', 161) <= 65535:
        return 'El puerto debe estar entre 1 y 65535.'
    if not editing and not data.get('community'):
        return 'Ingresá la community SNMP.'
    return None


@app.post('/api/hosts')
def create_host():
    data = request.get_json()
    error = check_host(data, editing=False)
    if error:
        return jsonify(error=error), 400
    with connect_db() as db, db.cursor() as cursor:
        cursor.execute(
            'INSERT INTO hosts (name, ip, port, community, enabled) VALUES (%s, %s, %s, %s, %s)',
            (data['name'].strip(), data['ip'], data.get('port', 161),
             data['community'], data.get('enabled', True)))
        return jsonify(id=cursor.lastrowid), 201


@app.put('/api/hosts/<int:host_id>')
def update_host(host_id):
    data = request.get_json()
    error = check_host(data, editing=True)
    if error:
        return jsonify(error=error), 400
    with connect_db() as db, db.cursor() as cursor:
        # Community vacía = conservar la actual. config_version+1 hace que
        # se descarten las mediciones tomadas con la configuración anterior.
        cursor.execute('''
            UPDATE hosts SET name=%s, ip=%s, port=%s, enabled=%s,
                community = IF(%s = '', community, %s),
                config_version = config_version + 1
            WHERE id=%s''',
            (data['name'].strip(), data['ip'], data.get('port', 161),
             data.get('enabled', True), data.get('community', ''),
             data.get('community', ''), host_id))
        if cursor.rowcount == 0:
            return jsonify(error='Host no encontrado.'), 404
    return jsonify(id=host_id)


@app.delete('/api/hosts/<int:host_id>')
def delete_host(host_id):
    # Las mediciones del host se borran solas (ON DELETE CASCADE en init.sql).
    with connect_db() as db, db.cursor() as cursor:
        cursor.execute('DELETE FROM hosts WHERE id=%s', (host_id,))
        if cursor.rowcount == 0:
            return jsonify(error='Host no encontrado.'), 404
    return '', 204


@app.get('/api/hosts/<int:host_id>/history')
def history(host_id):
    # Últimas 120 mediciones de la configuración actual, de la más vieja a la más nueva.
    with connect_db() as db, db.cursor() as cursor:
        cursor.execute('''
            SELECT s.checked_at, s.status, s.response_ms, s.cpu_percent, s.memory_percent
            FROM samples s JOIN hosts h ON h.id = s.host_id
            WHERE s.host_id = %s AND s.config_version = h.config_version
            ORDER BY s.id DESC LIMIT 120''', (host_id,))
        samples = cursor.fetchall()
    samples.reverse()
    return jsonify(samples=samples)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
