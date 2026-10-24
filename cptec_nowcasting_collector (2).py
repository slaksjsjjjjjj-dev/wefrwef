#!/usr/bin/env python3
"""Discover the CPTEC Nowcasting radar catalog and collect every listed full frame."""
from __future__ import annotations
import concurrent.futures, datetime as dt, hashlib, json, os, re, sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SITE = os.getenv('CPTEC_SITE_BASE', 'https://nowcasting.cptec.inpe.br/').rstrip('/') + '/'
API = os.getenv('CPTEC_API_BASE', 'https://nowcasting.cptec.inpe.br/api/v1').rstrip('/')
OUT = Path(os.getenv('CPTEC_OUTPUT_DIR', 'cptec-nowcasting-output'))
WORKERS = max(1, min(int(os.getenv('CPTEC_DOWNLOAD_WORKERS', '8')), 16))
TIMEOUT = max(5, int(os.getenv('CPTEC_REQUEST_TIMEOUT', '25')))
MAX_BYTES = 25 * 1024 * 1024
MAX_ROUTES = 160

SEED_ROUTES = [
    '/api/v1/paths/frames/radar/', '/api/v1/paths/frames/radar',
    '/api/v1/paths/frames/radar/index.json', '/api/v1/paths/frames/radar/catalog.json',
    '/api/v1/paths/frames/', '/api/v1/paths/', '/api/v1/paths',
    '/api/v1/radars', '/api/v1/radar', '/api/v1/radar/frames', '/api/v1/radar/products',
    '/api/v1/frames/radar', '/api/v1/frames', '/api/v1/products/radar', '/api/v1/products',
    '/api/v1/catalog/radar', '/api/v1/catalog', '/api/v1/metadata/radar', '/api/v1/metadata',
    '/api/v1/nowcasting/radars', '/api/v1/nowcasting/catalog', '/api/v1/openapi.json',
    '/openapi.json', '/swagger.json', '/frames/radar/index.json', '/data/frames/radar/index.json',
]
FRAME_RX = re.compile(r'(?:https?://[^\s\"\'<>]+)?/?(?:api/v1/paths/)?(frames/radar/[^\s\"\'<>?#]+)', re.I)
COMPOSITE_RX = re.compile(r'([A-Za-z0-9_.-]+~[A-Za-z0-9_.-]+~[A-Za-z0-9_.-]+~[A-Za-z0-9_.-]+)/(\d{12})/(?:full|original|large|medium|small|thumbnail)?', re.I)
API_RX = re.compile(r'[\"\'](\/api\/v1\/[A-Za-z0-9_./?=&%{}:$-]+)[\"\']')
SCRIPT_RX = re.compile(r'(?:src|href)=[\"\']([^\"\']+\.(?:m?js)(?:\?[^\"\']*)?)[\"\']', re.I)
JSON_RX = re.compile(r'(?:src|href)=[\"\']([^\"\']+\.(?:json|geojson)(?:\?[^\"\']*)?)[\"\']', re.I)

def session():
    s = requests.Session()
    retry = Retry(total=2, connect=2, read=1, backoff_factor=.3,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(['GET']), respect_retry_after_header=True)
    s.mount('https://', HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16))
    s.mount('http://', HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16))
    s.headers.update({'User-Agent':'Sideral-CPTEC-Nowcasting-Collector/1.0',
                      'Accept':'application/json,text/plain,image/*,*/*;q=0.8', 'Cache-Control':'no-cache'})
    return s

def utcnow(): return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

def get_text(s, url, limit=8*1024*1024, timeout=None):
    info = {'url':url}
    try:
        with s.get(url, timeout=timeout or TIMEOUT, stream=True) as r:
            info.update(status=r.status_code, content_type=r.headers.get('content-type',''))
            if r.status_code >= 400:
                info['error'] = f'HTTP {r.status_code}'; return None, info
            data = bytearray()
            for chunk in r.iter_content(65536):
                if chunk:
                    if len(data) + len(chunk) > limit:
                        info['truncated'] = True; break
                    data.extend(chunk)
            return bytes(data).decode(r.encoding or 'utf-8', errors='replace'), info
    except Exception as e:
        info['error'] = f'{type(e).__name__}: {e}'; return None, info

def canon(raw):
    if not isinstance(raw, str): return None
    raw = raw.strip().replace('\\u002F','/').replace('\\/','/').replace('&amp;','&')
    path = urlsplit(raw).path if raw.startswith(('http://','https://')) else raw.split('?',1)[0].split('#',1)[0]
    m = re.search(r'(?:^|/)api/v1/paths/(frames/radar/[^\s\"\'<>?#]+)', path, re.I)
    if not m: m = re.search(r'(?:^|/)(frames/radar/[^\s\"\'<>?#]+)', path, re.I)
    if m: tail = m.group(1).strip('/')
    else:
        m = COMPOSITE_RX.search(path)
        if not m: return None
        tail = f'frames/radar/{m.group(1)}/{m.group(2)}/full'
    bits = tail.split('/')
    if len(bits) < 4 or bits[0:2] != ['frames','radar'] or not re.fullmatch(r'[^/]+~[^/]+~[^/]+~[^/]+',bits[2]) or not re.fullmatch(r'\d{12}',bits[3]): return None
    return f'/api/v1/paths/frames/radar/{bits[2]}/{bits[3]}/full'

def extract(payload):
    refs=set()
    try: blob=json.dumps(payload,ensure_ascii=False,separators=(',',':'))
    except Exception: blob=str(payload)
    for rx in (FRAME_RX, COMPOSITE_RX):
        for m in rx.finditer(blob):
            ref=canon(m.group(0))
            if ref: refs.add(ref)
    def stamp(v):
        v=str(v)
        if re.fullmatch(r'\d{12}',v): return v
        m=re.match(r'^(\d{4})-?(\d{2})-?(\d{2})[T _]?(\d{2}):?(\d{2})',v)
        return ''.join(m.groups()) if m else None
    def walk(x):
        if isinstance(x,dict):
            d={str(k).lower().replace('-','_'):v for k,v in x.items()}
            for k in ('url','href','src','path','frame_url','image_url','full_url'):
                if isinstance(d.get(k),str):
                    r=canon(d[k])
                    if r: refs.add(r)
            composite=next((d[k] for k in ('id','key','product_id','path_id','product_path','slug','code','product') if isinstance(d.get(k),str) and re.fullmatch(r'[^/]+~[^/]+~[^/]+~[^/]+',d[k])),None)
            times=next((d[k] for k in ('timestamps','times','instants','frames','available_times','datetimes') if isinstance(d.get(k),list)),None)
            one=next((stamp(d[k]) for k in ('timestamp','time','datetime','instant','date','frame_time','valid_time') if d.get(k) is not None and stamp(d[k])),None)
            if composite:
                for t in (times if times is not None else ([one] if one else [])):
                    if isinstance(t,dict): t=next((t[k] for k in ('timestamp','time','datetime','instant') if t.get(k) is not None),None)
                    t=stamp(t) if t is not None else None
                    if t:
                        r=canon(f'frames/radar/{composite}/{t}/full')
                        if r: refs.add(r)
            else:
                provider=next((d[k] for k in ('provider','source','network','institution') if isinstance(d.get(k),str)),None)
                radar=next((d[k] for k in ('radar','site','station','radar_id') if isinstance(d.get(k),str)),None)
                group=next((d[k] for k in ('group','family','product_group','category') if isinstance(d.get(k),str)),None)
                variable=next((d[k] for k in ('variable','product_code','field','parameter','product_name') if isinstance(d.get(k),str)),None)
                if all(isinstance(v,str) and '~' not in v for v in (provider,radar,group,variable)):
                    for t in (times if times is not None else ([one] if one else [])):
                        if isinstance(t,dict): t=next((t[k] for k in ('timestamp','time','datetime','instant') if t.get(k) is not None),None)
                        t=stamp(t) if t is not None else None
                        if t:
                            r=canon(f'frames/radar/{provider}~{radar}~{group}~{variable}/{t}/full')
                            if r: refs.add(r)
            for v in x.values(): walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
    walk(payload)
    return refs

def discover():
    s=session(); diagnostics=[]; routes=set(SEED_ROUTES); json_assets=set()
    html,info=get_text(s,SITE,limit=4*1024*1024); diagnostics.append({'stage':'frontend_html',**info})
    if html:
        assets=[urljoin(SITE,p) for p in SCRIPT_RX.findall(html)][:40]
        json_assets.update(urljoin(SITE,p) for p in JSON_RX.findall(html))
        for asset in dict.fromkeys(assets):
            js,ji=get_text(s,asset); diagnostics.append({'stage':'frontend_js',**ji})
            if not js: continue
            for route in API_RX.findall(js):
                route=re.split(r'\$\{|\{[A-Za-z_]|:[A-Za-z_]',route,maxsplit=1)[0]
                if route.startswith('/api/v1/') and any(k in route.lower() for k in ('radar','frame','path','product','catalog','metadata','nowcast','instant','time')):
                    routes.add(route)
            json_assets.update(urljoin(SITE,p) for p in JSON_RX.findall(js) if p.startswith('/'))
    s.close()
    urls=[]
    for route in sorted(routes):
        if any(x in route for x in (chr(36)+'{','{',':radar',':product','undefined','null')): continue
        urls.append(urljoin(SITE,route))
        if route.rstrip('/') == '/api/v1/paths/frames/radar':
            urls += [urljoin(SITE,route.rstrip('/')+'?format=json'),urljoin(SITE,route.rstrip('/')+'/index.json')]
    urls += sorted(json_assets)
    return list(dict.fromkeys(urls))[:MAX_ROUTES], diagnostics

def catalog():
    urls, frontdiag=discover(); s=session(); refs=set(); diag=[]; i=0
    while i < len(urls) and i < MAX_ROUTES:
        url=urls[i]; i+=1
        body,info=get_text(s,url,limit=12*1024*1024,timeout=min(12,TIMEOUT))
        row={'stage':'catalog_probe',**info}
        if body:
            text=body.lstrip(); is_html='text/html' in info.get('content_type','').lower() or text.lower().startswith(('<!doctype html','<html'))
            if not is_html:
                try:
                    data=json.loads(body); found=extract(data); refs.update(found)
                    row['frame_refs_found']=len(found)
                    # OpenAPI discovery: enqueue useful static GET routes.
                    if isinstance(data,dict) and isinstance(data.get('paths'),dict):
                        for route,ops in data['paths'].items():
                            if isinstance(route,str) and isinstance(ops,dict) and any(str(k).lower()=='get' for k in ops) and '{' not in route and any(k in route.lower() for k in ('radar','frame','path','product','catalog','metadata','nowcast','instant','time')):
                                u=urljoin(SITE,route)
                                if u not in urls and len(urls)<MAX_ROUTES: urls.append(u)
                except json.JSONDecodeError:
                    found=set()
                    for rx in (FRAME_RX,COMPOSITE_RX):
                        for m in rx.finditer(body):
                            r=canon(m.group(0))
                            if r: found.add(r)
                    refs.update(found); row['frame_refs_found']=len(found)
                    if not found: row['preview']=body[:180]
            else: row['shape']='html'
        diag.append(row)
        if len(refs)>100000: break
    s.close()
    return refs, {'started_at':utcnow(),'site':SITE,'api_base':API,'catalog_urls_tested':i,'frontend_diagnostics':frontdiag,'catalog_diagnostics':diag}

def parse_ref(ref):
    m=re.search(r'frames/radar/([^/]+)/([0-9]{12})/full$',ref)
    p=m.group(1).split('~') if m else ['unknown']*4
    return {'api_path':ref,'product_id':'~'.join(p),'provider':p[0],'radar':p[1],'group':p[2],'variable':p[3],'timestamp':m.group(2) if m else 'unknown'}

def safe(s): return re.sub(r'[^A-Za-z0-9_.-]+','_',str(s)).strip('._')[:100] or 'unknown'

def image_ext(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return '.png'
    if data.startswith(b'\xff\xd8\xff'): return '.jpg'
    if data.startswith((b'GIF87a',b'GIF89a')): return '.gif'
    if len(data)>=12 and data[:4]==b'RIFF' and data[8:12]==b'WEBP': return '.webp'
    return None

def download(ref):
    meta=parse_ref(ref); url=urljoin(SITE,ref); s=session()
    try:
        with s.get(url,timeout=TIMEOUT,stream=True) as r:
            if r.status_code>=400: return {**meta,'url':url,'ok':False,'error':f'HTTP {r.status_code}'}
            data=bytearray()
            for chunk in r.iter_content(128*1024):
                if chunk:
                    if len(data)+len(chunk)>MAX_BYTES: return {**meta,'url':url,'ok':False,'error':'frame exceeds 25 MiB'}
                    data.extend(chunk)
            ext=image_ext(data)
            if not ext: return {**meta,'url':url,'ok':False,'error':'response is not an image','content_type':r.headers.get('content-type',''),'preview':bytes(data[:180]).decode('utf-8',errors='replace')}
            rel=Path('frames')/safe(meta['provider'])/safe(meta['radar'])/safe(meta['group'])/safe(meta['variable'])/(meta['timestamp']+ext)
            target=OUT/rel; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(data)
            return {**meta,'url':url,'ok':True,'file':rel.as_posix(),'bytes':len(data),'content_type':r.headers.get('content-type',''),'sha256':hashlib.sha256(data).hexdigest()}
    except Exception as e: return {**meta,'url':url,'ok':False,'error':f'{type(e).__name__}: {e}'}
    finally: s.close()

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    catalog_input=os.getenv('CPTEC_CATALOG_INPUT')
    if catalog_input:
        catalog_data=json.loads(Path(catalog_input).read_text(encoding='utf-8'))
        all_refs=set(catalog_data.get('refs',[]))
        discovery=catalog_data.get('discovery',{})
    else:
        all_refs,discovery=catalog()
    ordered_refs=sorted(all_refs)
    catalog_count=len(ordered_refs)
    shard_count=max(1,min(int(os.getenv('CPTEC_SHARD_COUNT','1')),16))
    shard_index=int(os.getenv('CPTEC_SHARD_INDEX','0'))
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError(f'CPTEC_SHARD_INDEX={shard_index} inválido para CPTEC_SHARD_COUNT={shard_count}')
    refs={ref for ref in ordered_refs if int(hashlib.sha256(ref.encode('utf-8')).hexdigest()[:8],16) % shard_count == shard_index}
    discovery['candidate_frames']=catalog_count
    discovery['shard']={'index':shard_index,'count':shard_count,'assigned_frames':len(refs)}
    discovery['generated_at']=utcnow()
    (OUT/'discovery.json').write_text(json.dumps(discovery,ensure_ascii=False,indent=2),encoding='utf-8')
    if os.getenv('CPTEC_DISCOVERY_ONLY','0') == '1':
        (OUT/'catalog.json').write_text(json.dumps({'refs':ordered_refs,'discovery':discovery},ensure_ascii=False,indent=2),encoding='utf-8')
        print(f'Catálogo descoberto: {catalog_count} frames.')
        if not all_refs:
            print('ATENÇÃO: nenhum frame encontrado; veja discovery.json.',file=sys.stderr)
        return 0
    if not all_refs:
        (OUT/'index.json').write_text(json.dumps({'schema':'sideral.cptec-index.v1','source':'CPTEC/INPE Nowcasting','generated_at':utcnow(),'frames':[],'error':'Nenhum frame encontrado; consulte discovery.json.','summary':{'catalog_frames_discovered':0,'shard_index':shard_index,'shard_count':shard_count}},ensure_ascii=False,indent=2),encoding='utf-8')
        (OUT/'errors.json').write_text(json.dumps({'generated_at':utcnow(),'count':0,'errors':[]},ensure_ascii=False,indent=2),encoding='utf-8')
        print('ERRO: nenhum frame encontrado nos endpoints/catalogos descobertos.',file=sys.stderr); return 2
    print(f'Catálogo: {catalog_count} frames; partição {shard_index+1}/{shard_count}: {len(refs)} frames; {WORKERS} workers.')
    results=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures=[pool.submit(download,r) for r in sorted(refs)]
        for n,f in enumerate(concurrent.futures.as_completed(futures),1):
            results.append(f.result())
            if n%25==0 or n==len(futures): print(f'Progresso: {n}/{len(futures)}')
    results.sort(key=lambda x:(x.get('provider',''),x.get('radar',''),x.get('product_id',''),x.get('timestamp','')))
    good=[r for r in results if r.get('ok')]; bad=[r for r in results if not r.get('ok')]
    index={'schema':'sideral.cptec-nowcasting-index.v1','source':'CPTEC/INPE Nowcasting','source_url':SITE,'generated_at':utcnow(),
           'summary':{'catalog_frames_discovered':catalog_count,'frames_discovered':len(refs),'frames_downloaded':len(good),'frames_failed':len(bad),
                      'shard_index':shard_index,'shard_count':shard_count,
                      'providers':sorted({x['provider'] for x in results}),
                      'radars':sorted({x['provider']+'~'+x['radar'] for x in results}),'products':sorted({x['product_id'] for x in results}),
                      'timestamps':sorted({x['timestamp'] for x in results}),'bytes_downloaded':sum(x.get('bytes',0) for x in good)},'frames':good}
    (OUT/'index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'errors.json').write_text(json.dumps({'generated_at':utcnow(),'count':len(bad),'errors':bad},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(index['summary'],ensure_ascii=False,indent=2))
    # An empty shard is expected when the live catalog contains fewer frames than shards.
    if not refs: return 0
    if not good: print('ERRO: esta partição não conseguiu baixar nenhum frame.',file=sys.stderr); return 3
    return 0

if __name__=='__main__': raise SystemExit(main())
