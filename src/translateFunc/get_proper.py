import requests

def fetch(min_len:int = 0):
    r=requests.get(f"https://web.lcta.top/proper_terms_paratranz.json",timeout=10)
    r.raise_for_status()
    r = r.json()
    return r
