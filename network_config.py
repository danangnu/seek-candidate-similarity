"""Explicit remote service configuration; no implicit server selection or redirects."""
import json
import math
import os
import re
import ssl
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler


def positive_timeout(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(name+' must be a positive number of seconds')
    return value


def database_options(config, password):
    host = config.get('host')
    if not isinstance(host, str) or not host or re.search(r'[\s/@?#\\]', host):
        raise ValueError('database.host must be a hostname or IP address, without a URL or port')
    database = config.get('database')
    if not isinstance(database, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,63}', database):
        raise ValueError('database.database must be an explicit database name, up to 64 letters/digits/_/-')
    port = config.get('port', 3306)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('database.port must be an integer from 1 to 65535')
    if not isinstance(config.get('user'), str) or not config['user'].strip():
        raise ValueError('database.user is required')
    options = dict(host=host, port=port, user=config['user'], password=password, database=database,
                   charset='utf8mb4', autocommit=True,
                   connect_timeout=positive_timeout(config.get('connect_timeout', 10), 'database.connect_timeout'),
                   read_timeout=positive_timeout(config.get('read_timeout', 60), 'database.read_timeout'),
                   write_timeout=positive_timeout(config.get('write_timeout', 60), 'database.write_timeout'))
    tls = config.get('tls', {})
    if not isinstance(tls, dict) or type(tls.get('enabled', False)) is not bool:
        raise ValueError('database.tls.enabled must be true or false')
    if tls.get('enabled', False):
        context = ssl.create_default_context(cafile=tls.get('ca_file') or None)
        if bool(tls.get('cert_file')) != bool(tls.get('key_file')):
            raise ValueError('Client TLS requires both database.tls.cert_file and key_file')
        if tls.get('cert_file'):
            context.load_cert_chain(tls['cert_file'], tls['key_file'])
        options['ssl'] = context  # Certificate and hostname verification remain enabled.
    elif any(tls.get(key) for key in ('ca_file', 'cert_file', 'key_file')):
        raise ValueError('Set database.tls.enabled=true to use the supplied TLS files')
    return options


def ollama_options(config):
    return {key: config[key] for key in ('endpoint', 'model', 'timeout', 'ca_file', 'api_key_env') if key in config}


def validate_endpoint(endpoint):
    if not isinstance(endpoint, str) or any(c.isspace() for c in endpoint):
        raise ValueError('ollama.endpoint must be an HTTP(S) base URL without whitespace')
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise ValueError('Invalid ollama.endpoint URL or port') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or '\\' in endpoint
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('ollama.endpoint must be an HTTP(S) base URL without credentials, query or fragment')
    return endpoint.rstrip('/')


class NoServiceRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Ollama redirected the request. Configure its final endpoint; redirects are not followed.')


def ollama_json(endpoint, path, payload=None, *, timeout=180, ca_file=None, api_key_env=None):
    endpoint = validate_endpoint(endpoint)
    positive_timeout(timeout, 'ollama.timeout')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if api_key_env:
        if not isinstance(api_key_env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', api_key_env):
            raise ValueError('ollama.api_key_env must name an environment variable')
        secret = os.environ.get(api_key_env)
        if not secret or '\r' in secret or '\n' in secret:
            raise ValueError('The configured Ollama API-key environment variable is missing or invalid')
        if not endpoint.startswith('https://'):
            raise ValueError('Use an HTTPS Ollama gateway when configuring an API key')
        headers['Authorization'] = 'Bearer '+secret
    handlers = [NoServiceRedirects()]
    if ca_file:
        if not endpoint.startswith('https://'):
            raise ValueError('ollama.ca_file requires an HTTPS endpoint')
        handlers.append(HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
    request = Request(endpoint+path, data=None if payload is None else json.dumps(payload).encode('utf-8'), headers=headers)
    with build_opener(*handlers).open(request, timeout=timeout) as response:
        return json.load(response)


def check_ollama(endpoint='http://127.0.0.1:11434', model='llama3.1:8b', **options):
    # No candidate data or generation request is sent during this check.
    data = ollama_json(endpoint, '/api/tags', **options)
    models = data.get('models') if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise ValueError('Ollama /api/tags returned an unexpected response')
    names = {m.get('name') or m.get('model') for m in models if isinstance(m, dict)}
    expected = model if ':' in model else model+':latest'
    if model not in names and expected not in names:
        raise ValueError('The configured model is not listed by the selected Ollama server')
    return {'model': model, 'endpoint': validate_endpoint(endpoint), 'model_available': True}
