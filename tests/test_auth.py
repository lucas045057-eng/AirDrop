def test_health_and_login(client):
    assert client.get('/api/health').json()['status'] == 'ok'
    assert client.get('/api/auth/me').status_code == 401
    assert client.post('/api/auth/login', json={'username':'admin','password':'wrong'}).status_code == 401


def test_csrf_and_logout(logged):
    assert logged.get('/api/auth/me').json()['role'] == 'admin'
    csrf = logged.headers.pop('x-csrf-token')
    assert logged.post('/api/auth/logout').status_code == 403
    logged.headers['x-csrf-token'] = csrf
    assert logged.post('/api/auth/logout').status_code == 200
    assert logged.get('/api/auth/me').status_code == 401
