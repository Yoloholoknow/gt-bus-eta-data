import requests

BASE_URL = "https://bus.gatech.edu/Services/JSONPRelay.svc"


def get(method, **params):
    resp = requests.get(f"{BASE_URL}/{method}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()
