"""Foreground trusted-team rooms; native OpenCode stays on each owner's machine."""
from __future__ import annotations
import argparse, asyncio, base64, contextlib, getpass, hashlib, hmac, ipaddress, json, os, re, secrets
import signal, socket, ssl, subprocess, sys, tempfile, time
from urllib.parse import urlsplit
from pathlib import Path


def name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',value):
        raise ValueError('Use a short name containing letters, digits, dots, underscores or hyphens.')
    return value


def endpoint(value):
    if value is None:return None
    if not isinstance(value,dict) or set(value)!={'host','port','fingerprint'}:raise ValueError('Invalid shared endpoint.')
    try: address=ipaddress.ip_address(value['host'])
    except (ValueError,TypeError):raise ValueError('Shared endpoints need literal private IPv4 addresses.') from None
    if address.version!=4 or not (address.is_private or address in ipaddress.ip_network('100.64.0.0/10')) or address.is_unspecified or address.is_multicast:
        raise ValueError('Shared endpoints need private IPv4 addresses.')
    if type(value['port']) is not int or not 1<=value['port']<=65535 or not re.fullmatch('[0-9a-f]{64}',str(value['fingerprint'])):
        raise ValueError('Invalid shared endpoint port or certificate fingerprint.')
    return dict(value)


class RoomState:
    def __init__(self, owner, code, now=time.monotonic):
        self.now=now;self.owner=name(owner);self.code=code;self.expires=now()+600
        self.owner_token=secrets.token_urlsafe(32)
        self.members={'self':{'id':'self','name':owner,'token':self.owner_token,'peer':None,'seen':now()}}
        self.pending={}
    def request_join(self,code,member,peer):
        if self.now()>self.expires or not isinstance(code,str) or not hmac.compare_digest(code,self.code):raise ValueError('Invitation expired or invalid.')
        name(member);peer=endpoint(peer)
        self.pending={k:v for k,v in self.pending.items() if self.now()-v['created']<600}
        if sum(x['status']=='pending' for x in self.pending.values())>=32:raise ValueError('Room invitation queue is full.')
        if any(x['name']==member for x in self.members.values()) or any(x['name']==member and x['status']=='pending' for x in self.pending.values()):raise ValueError('That room name is already in use.')
        item={'id':secrets.token_hex(8),'ticket':secrets.token_urlsafe(32),'name':member,'peer':peer,'status':'pending','created':self.now()}
        self.pending[item['id']]=item
        return {'id':item['id'],'ticket':item['ticket'],'status':'pending'}
    def join_status(self,ident,ticket):
        item=self.pending.get(ident)
        if not item or not isinstance(ticket,str) or not hmac.compare_digest(ticket,item['ticket']):raise ValueError('Unknown invitation.')
        return {k:item[k] for k in ['status','token'] if k in item}
    def approve(self,ident):
        item=self.pending.get(ident)
        if not item or item['status']!='pending' or self.now()-item['created']>600:raise ValueError('Invitation is not pending or expired.')
        item['status']='approved';item['token']=secrets.token_urlsafe(32)
        member={k:item[k] for k in ['id','name','peer','token']};member['seen']=self.now();self.members[ident]=member
        return member
    def reject(self,ident):
        if ident not in self.pending:raise ValueError('Unknown invitation.')
        self.pending[ident]['status']='rejected';self.pending[ident].pop('token',None)
    def revoke(self,ident):
        if ident=='self' or ident not in self.members:raise ValueError('Unknown member.')
        self.members.pop(ident);self.reject(ident)
    def authorized(self,token):
        return isinstance(token,str) and any(hmac.compare_digest(token,m['token']) for m in self.members.values())
    def touch(self,token):
        for member in self.members.values():
            if hmac.compare_digest(token,member['token']):member['seen']=self.now();return
    def peers(self):
        return [{'id':m['id'],'name':m['name'],**m['peer']} for m in self.members.values() if m['peer'] and (m['id']=='self' or self.now()-m['seen']<20)]


def bearer(request):
    raw=request.headers.get('Authorization','')
    if raw.startswith('Bearer '):return raw[7:]
    if raw.startswith('Basic '):
        try:
            user,token=base64.b64decode(raw[6:],validate=True).decode().split(':',1)
            return token if user=='opencode' else ''
        except (ValueError,UnicodeError):return ''
    return ''


class RoomNode:
    def __init__(self, owner, shared=False, upstream='http://127.0.0.1:49374', password='', state=None, ui_events=False):
        parsed=urlsplit(upstream)
        if parsed.scheme!='http' or parsed.hostname not in ['127.0.0.1','localhost','::1'] or parsed.username or parsed.password or parsed.path not in ['', '/'] or parsed.query or parsed.fragment:
            raise ValueError('The shared native server must be a plain loopback HTTP URL.')
        self.owner=name(owner);self.shared=shared;self.upstream=upstream.rstrip('/');self.password=password;self.state=state
        self.token=state.owner_token if state else None;self.coordinator=None;self.client=None;self.ui_events=ui_events
        self.control_token=secrets.token_urlsafe(32);self.control_url=None;self.runners=[];self.peers_cache=[];self.rates={};self.stopping=asyncio.Event()
    async def remote(self,peer,method,path,body=None,token=None):
        import aiohttp
        url=f"https://{peer['host']}:{peer['port']}{path}"
        response=await self.client.request(method,url,json=body,headers={'Authorization':'Bearer '+(token or self.token or '')},ssl=aiohttp.Fingerprint(bytes.fromhex(peer['fingerprint'])),timeout=aiohttp.ClientTimeout(total=10))
        async with response:
            if response.status>=400:raise ValueError(f'Room request failed with HTTP {response.status}.')
            return await response.json()
    async def allowed(self,token):
        if self.state:return self.state.authorized(token)
        if not self.coordinator:return False
        try:await self.remote(self.coordinator,'GET','/v1/check',token=token);return True
        except Exception:return False
    async def peers(self):
        if self.state:return self.state.peers()
        return (await self.remote(self.coordinator,'GET','/v1/peers'))['peers']
    async def network(self,request):
        from aiohttp import web
        path=request.path
        if self.state and path=='/v1/join' and request.method=='POST':
            key=request.remote or '';now=time.monotonic();recent=[x for x in self.rates.get(key,[]) if now-x<60]
            if len(recent)>=10:return web.json_response({'error':'Too many invitations; retry later.'},status=429)
            recent.append(now);self.rates[key]=recent
            body=await request.json();peer=endpoint(body.get('peer'))
            # A member may publish only the address from which it joined (no LAN SSRF).
            if peer and peer['host']!=request.remote:raise ValueError('Shared endpoint must match the joining device address.')
            item=self.state.request_join(body.get('code'),body.get('name'),peer)
            if self.ui_events:
                print('APP_WORKFLOW_EVENT '+json.dumps({'event':'join-request','id':item['id'],'name':body['name']}),flush=True)
            else:
                print(f"Join request: {body['name']} [{item['id']}]. Type approve {item['id']} or reject {item['id']}.",flush=True)
            return web.json_response(item)
        if self.state and path.startswith('/v1/join/') and request.method=='GET':
            return web.json_response(self.state.join_status(path.rsplit('/',1)[1],bearer(request)))
        token=bearer(request)
        if not await self.allowed(token):raise web.HTTPUnauthorized()
        if path=='/v1/check':return web.json_response({'ok':True})
        if self.state and path=='/v1/heartbeat' and request.method=='POST':self.state.touch(token);return web.json_response({'ok':True})
        if self.state and path=='/v1/peers':return web.json_response({'peers':self.state.peers()})
        if path.startswith('/opencode/') and self.shared:
            native_path=path[len('/opencode'):]
            if request.method=='GET' and native_path=='/api/config':return web.json_response([])
            empty_lists=['/api/integration','/api/mcp','/api/provider','/api/reference','/api/shell','/api/form','/api/plugin']
            if request.method=='GET' and native_path in [*empty_lists,'/api/mcp/resource']:
                import aiohttp
                headers={'Authorization':'Basic '+base64.b64encode(('opencode:'+self.password).encode()).decode()}
                async with self.client.get(self.upstream+'/api/location',headers=headers,timeout=aiohttp.ClientTimeout(total=5)) as local:
                    location=await local.json()
                return web.json_response({'location':location,'data':{'resources':[],'templates':[]} if native_path=='/api/mcp/resource' else []})
            return await self.proxy(request,self.upstream,path[len('/opencode'):],self.password,lambda:self.allowed(token),native=True)
        raise web.HTTPNotFound()
    async def control(self,request):
        from aiohttp import web
        if not hmac.compare_digest(bearer(request),self.control_token):raise web.HTTPUnauthorized()
        if request.path=='/v1/stop' and request.method=='POST':
            self.stopping.set();return web.json_response({'ok':True})
        if request.path=='/v1/peers':
            peers=await self.peers()
            return web.json_response({'peers':[{'id':x['id'],'name':x['name'],'url':self.control_url+'/peer/'+x['id'],'token':self.control_token} for x in peers]})
        if request.path=='/v1/members' and not self.state:
            return web.json_response({'members':[{'id':x['id'],'name':x['name']} for x in await self.peers()],'role':'member'})
        if request.path=='/v1/members' and self.state:
            return web.json_response({'members':[{'id':x['id'],'name':x['name']} for x in self.state.members.values()],'pending':[{'id':x['id'],'name':x['name']} for x in self.state.pending.values() if x['status']=='pending']})
        if request.method=='POST' and self.state and request.path in ['/v1/approve','/v1/reject','/v1/revoke']:
            body=await request.json();getattr(self.state,request.path.rsplit('/',1)[1])(body['id']);return web.json_response({'ok':True})
        if request.path.startswith('/peer/'):
            parts=request.path.split('/',3)
            if len(parts)<4:raise web.HTTPNotFound()
            peer=next((x for x in await self.peers() if x['id']==parts[2]),None)
            if not peer:raise web.HTTPNotFound()
            return await self.proxy(request,f"https://{peer['host']}:{peer['port']}",'/opencode/'+parts[3],self.token,lambda:self.allowed(self.token),fingerprint=peer['fingerprint'])
        raise web.HTTPNotFound()
    async def proxy(self,request,base,path,credential,authorized,native=False,fingerprint=None):
        import aiohttp
        from aiohttp import web
        # Never expose auth storage, integrations, configuration documents or credential mutation.
        if not clean_path(path):raise web.HTTPForbidden(reason='Noncanonical API path.')
        if native and not permitted_native(request.method,path):raise web.HTTPForbidden(reason='This endpoint is not shared by team rooms.')
        headers={k:v for k,v in request.headers.items() if k.lower() not in {'authorization','host','connection','upgrade','proxy-authorization','cookie','content-length','transfer-encoding','accept-encoding'}}
        headers['Authorization']=('Basic '+base64.b64encode(('opencode:'+credential).encode()).decode()) if native else 'Bearer '+credential
        target=base+path+('?' + request.query_string if request.query_string else '')
        ssl_option=aiohttp.Fingerprint(bytes.fromhex(fingerprint)) if fingerprint else None
        if request.headers.get('Upgrade','').lower()=='websocket':raise web.HTTPNotImplemented(reason='Remote PTY/WebSocket sharing is disabled in this version.')
        body=await request.read()
        connecting=asyncio.ensure_future(self.client.request(request.method,target,data=body,headers=headers,ssl=ssl_option,allow_redirects=False,timeout=aiohttp.ClientTimeout(total=None,sock_connect=10)))
        try:
            while True:
                done,_=await asyncio.wait([connecting],timeout=1)
                if not await authorized():raise web.HTTPUnauthorized()
                if done:break
            upstream=connecting.result()
        finally:
            if not connecting.done():
                connecting.cancel();await asyncio.gather(connecting,return_exceptions=True)
        async with upstream:
            response=web.StreamResponse(status=upstream.status,headers={k:v for k,v in upstream.headers.items() if k.lower() not in {'connection','transfer-encoding','content-length','set-cookie','content-encoding'}})
            await response.prepare(request)
            iterator=upstream.content.iter_any().__aiter__();pending=None
            try:
                while True:
                    if pending is None:pending=asyncio.create_task(iterator.__anext__())
                    done,_=await asyncio.wait([pending],timeout=1)
                    if not await authorized():break
                    if not done:continue
                    try:chunk=pending.result()
                    except StopAsyncIteration:break
                    pending=None;await response.write(chunk)
            except (ConnectionError,asyncio.CancelledError):pass
            finally:
                if pending and not pending.done():pending.cancel()
                await response.write_eof()
            return response
    async def start(self,host,cert,key):
        import aiohttp
        from aiohttp import web
        self.client=aiohttp.ClientSession()
        @web.middleware
        async def safe_errors(request,handler):
            try:return await handler(request)
            except (ValueError,KeyError,TypeError):return web.json_response({'error':'Invalid room request or connection state.'},status=400)
            except (aiohttp.ClientError,asyncio.TimeoutError):return web.json_response({'error':'Peer is unreachable or its certificate changed.'},status=502)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
        async def serve(handler,bind,ssl_context=None,port=0):
            app=web.Application(middlewares=[safe_errors],client_max_size=8*1024*1024);app.router.add_route('*','/{tail:.*}',handler)
            runner=web.AppRunner(app,access_log=None);await runner.setup();self.runners.append(runner)
            site=web.TCPSite(runner,bind,port,ssl_context=ssl_context);await site.start();return site._server.sockets[0].getsockname()[1]
        hosts=host if isinstance(host,list) else [host]
        network_port=await serve(self.network,hosts[0],context)
        for address in hosts[1:]:await serve(self.network,address,context,network_port)
        local_port=await serve(self.control,'127.0.0.1');self.control_url=f'http://127.0.0.1:{local_port}'
        return network_port
    async def close(self):
        for runner in reversed(self.runners):await runner.cleanup()
        if self.client:await self.client.close()


def clean_path(path):
    return isinstance(path,str) and path.startswith('/') and not any(x in ['.','..'] for x in path.split('/')) and '%' not in path and '\\' not in path


def permitted_native(method,path):
    if not clean_path(path):return False
    # Session operations retain the host's approval policy. Server/global mutations are excluded.
    if path=='/api/session' or path.startswith('/api/session/'):return True
    if method!='GET':return False
    return path=='/api/event' or any(path==prefix or path.startswith(prefix+'/') for prefix in ['/api/location','/api/project','/api/file','/api/agent','/api/model','/api/skill','/api/command','/api/permission','/api/server','/api/fs','/api/info','/api/vcs','/api/experimental/migration/v1'])


def runtime_path():
    root=Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'app-workflow'
    path=root/'room.json'
    if any(x.is_symlink() for x in [path,*path.parents]):raise ValueError('Room runtime paths must not be symlinks.')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if root.stat().st_mode&0o077:raise ValueError('Room runtime directory must have private permissions.')
    return path


def publish_runtime(node):
    path=runtime_path()
    descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'w') as out:json.dump({'version':1,'url':node.control_url,'token':node.control_token,'pid':os.getpid()},out)
    return path


async def console(node):
    if not node.ui_events:
        print('Room running. Commands: members; approve ID; reject ID; revoke ID; quit. Ctrl+C also stops it.',flush=True)
    queue=asyncio.Queue();loop=asyncio.get_running_loop()
    def ready():
        line=sys.stdin.readline()
        if line:queue.put_nowait(line.strip())
        else:loop.remove_reader(sys.stdin.fileno());queue.put_nowait('quit')
    if not sys.stdin.isatty():
        await asyncio.Event().wait();return
    loop.add_reader(sys.stdin.fileno(),ready)
    try:
        while True:
            command=await queue.get()
            if command in ['quit','exit']:return
            if command=='members' and node.state:
                print(', '.join(f"{x['name']} [{x['id']}]" for x in node.state.members.values()),flush=True);continue
            parts=command.split()
            if len(parts)==2 and parts[0] in ['approve','reject','revoke'] and node.state:
                try:getattr(node.state,parts[0])(parts[1]);print('Updated room membership.',flush=True)
                except ValueError as e:print(str(e),flush=True)
            else:print('Use members, approve ID, reject ID, revoke ID or quit.',flush=True)
    finally:loop.remove_reader(sys.stdin.fileno())


async def run_room(args):
    from .room_transport import create_certificate,generate_join_code,DiscoveryAnnouncer,discover,host_candidates,tailscale_status
    import aiohttp
    candidates=host_candidates();tail=tailscale_status() if args.transport in ['auto','tailscale'] else None
    if args.transport=='tailscale' and (not tail or tail.get('state')!='Running'):raise ValueError('Tailscale must already be installed and logged in; no enrollment was changed.')
    lan_addresses=[x for x in candidates if not x.startswith('127.') and not x.startswith('100.')]
    tailscale_addresses=tail.get('addresses',[]) if tail and tail.get('state')=='Running' else []
    if args.transport=='tailscale':addresses=tailscale_addresses
    elif args.transport=='auto':addresses=lan_addresses or tailscale_addresses
    else:addresses=lan_addresses
    bind=args.bind or (addresses[0] if addresses else '127.0.0.1')
    if bind=='127.0.0.1' and not args.bind:raise ValueError('No private network interface found. Select --bind explicitly or connect Tailscale.')
    endpoint({'host':bind,'port':1,'fingerprint':'0'*64})
    owner=args.name or re.sub('[^A-Za-z0-9_.-]','-',socket.gethostname())[:64]
    if not args.share and sys.stdin.isatty():
        args.share=input('Share this whole local OpenCode instance with approved teammates? [y/N] ').strip().lower()=='y'
    if runtime_path().exists():raise ValueError('A room runtime already exists. Stop the active room before starting another.')
    hosting=args.room_action=='host';code=generate_join_code() if hosting else args.code
    state=RoomState(owner,code) if hosting else None
    password=os.environ.get('OPENCODE_PASSWORD','')
    if args.share and not password:
        result=subprocess.run(['opencode','service','get','password'],capture_output=True,text=True,timeout=10)
        if result.returncode:raise ValueError('Cannot read native local server password. Set OPENCODE_PASSWORD privately.')
        password=result.stdout.strip()
    node=RoomNode(owner,args.share,args.upstream,password,state,args.ui_events)
    runtime=None;announcer=None
    with tempfile.TemporaryDirectory(prefix='app-workflow-room-') as temporary:
        cert,key,fingerprint=create_certificate(Path(temporary))
        try:
            hosts=list(dict.fromkeys([bind,*(tail.get('addresses',[]) if args.transport=='auto' and tail and tail.get('state')=='Running' else [])]))
            port=await node.start(hosts,cert,key);own={'host':bind,'port':port,'fingerprint':fingerprint}
            if hosting:
                if args.share:state.members['self']['peer']=own
                announcer=DiscoveryAnnouncer(code,bind,port,fingerprint,bind_host='0.0.0.0' if bind!='127.0.0.1' else bind,alternate_host=next((x for x in hosts if x.startswith('100.')),None))
                announcer.__enter__()
                if args.ui_events:
                    print('APP_WORKFLOW_EVENT '+json.dumps({'event':'room-ready','name':owner,'code':code,'expires_in':600}),flush=True)
                else:
                    print(f'Room: {owner}. Invitation expires in10minutes. Host approval required.\nJoin code: {code}',flush=True)
            else:
                targets=[None] if args.transport!='tailscale' else []
                if args.address:targets=[(args.address,49377)]
                elif tail and tail.get('state')=='Running':targets += [(x,49377) for x in tail.get('peer_addresses',[])]
                adverts=[]
                for target in targets:
                    found=await asyncio.to_thread(discover,code,timeout=2,target=target)
                    adverts.extend(found)
                    if adverts:break
                if not adverts:raise ValueError('Room not found. Check Wi-Fi isolation, Tailscale, or use --address HOST.')
                node.coordinator=endpoint({k:adverts[0][k] for k in ['host','port','fingerprint']})
                if node.coordinator['host'].startswith('100.'):
                    own['host']=next((x for x in hosts if x.startswith('100.')),bind)
                pending=await node.remote(node.coordinator,'POST','/v1/join',{'code':code,'name':owner,'peer':own if args.share else None})
                if args.ui_events:
                    print('APP_WORKFLOW_EVENT '+json.dumps({'event':'join-pending','name':owner}),flush=True)
                else:
                    print('Waiting for host approval. No instance is shared until approved.',flush=True)
                deadline=time.monotonic()+600
                while time.monotonic()<deadline:
                    answer=await node.remote(node.coordinator,'GET','/v1/join/'+pending['id'],token=pending['ticket'])
                    if answer['status']=='approved':node.token=answer['token'];break
                    if answer['status']=='rejected':raise ValueError('Host rejected or revoked this invitation.')
                    await asyncio.sleep(1)
                if not node.token:raise ValueError('Host approval timed out.')
                if args.ui_events:
                    print('APP_WORKFLOW_EVENT '+json.dumps({'event':'room-joined','name':owner}),flush=True)
                else:
                    print('Joined room. Use the native team picker to switch sessions.',flush=True)
            runtime=publish_runtime(node)
            async def heartbeat():
                while True:
                    await asyncio.sleep(3)
                    if not hosting:await node.remote(node.coordinator,'POST','/v1/heartbeat',{})
            pulse=asyncio.create_task(heartbeat());interaction=asyncio.create_task(console(node));stopping=asyncio.create_task(node.stopping.wait())
            try:
                done,_=await asyncio.wait([pulse,interaction,stopping],return_when=asyncio.FIRST_COMPLETED)
                for task in done:task.result()
            finally:
                for task in [pulse,interaction,stopping]:task.cancel()
                await asyncio.gather(pulse,interaction,stopping,return_exceptions=True)
        finally:
            if announcer:announcer.__exit__(None,None,None)
            if runtime:
                try:
                    if not runtime.is_symlink() and json.loads(runtime.read_text()).get('token')==node.control_token:runtime.unlink()
                except (OSError,ValueError):pass
            await node.close()


def configure_room_parser(commands, include_open=True):
    actions = ['status','members','approve','reject','revoke','stop']
    if include_open:
        actions.append('open')
    for action in actions:
        p=commands.add_parser(action,help='manage the local active room');p.set_defaults(room_action=action)
        if action in ['approve','reject','revoke']:p.add_argument('id')
    for action in ['host','join']:
        p=commands.add_parser(action,help='start a foreground trusted-team room' if action=='host' else 'join a room by invitation code')
        p.set_defaults(room_action=action)
        if action=='join':
            p.add_argument('code',nargs='?',help='omit to enter the code privately at a prompt')
            p.add_argument('--address',help='known room host address if discovery is blocked')
            p.add_argument('--code-stdin',action='store_true',help=argparse.SUPPRESS)
        p.add_argument('--name');p.add_argument('--transport',choices=['auto','lan','tailscale'],default='auto')
        p.add_argument('--bind',help='private local IPv4 interface to use')
        p.add_argument('--share',action='store_true',help='explicitly share this whole local OpenCode instance with approved members')
        p.add_argument('--upstream',default='http://127.0.0.1:49374',help='local native server URL')
        p.add_argument('--ui-events',action='store_true',help=argparse.SUPPRESS)


def dispatch_room(args):
    try:
        if args.room_action=='open':
            from .cli import SOURCE_ROOT
            launcher=SOURCE_ROOT/'integration/team-tui/launch.sh'
            if not launcher.is_file():raise ValueError('Native team launcher is missing; see docs/NATIVE-TEAM.md.')
            os.execv(str(launcher),[str(launcher)])
        if args.room_action in ['status','members','approve','reject','revoke','stop']:
            return local_action(args)
        if args.room_action=='join' and args.code_stdin:
            if args.code:raise ValueError('Do not pass a room code both as an argument and on standard input.')
            from .room_transport import parse_join_code
            value=sys.stdin.readline(128)
            if not value.endswith('\n'):raise ValueError('Room code input was missing or too long.')
            args.code=value.rstrip('\r\n')
            parse_join_code(args.code)
        elif args.room_action=='join' and not args.code:
            if not sys.stdin.isatty():raise ValueError('Run this in a terminal and enter the code at the private prompt.')
            args.code=getpass.getpass('Team room code: ')
        asyncio.run(run_room(args));return 0
    except ImportError:raise ValueError('Team rooms require aiohttp. Install the toolkit room extra in a dedicated environment.') from None


def local_action(args):
    import urllib.request
    path=runtime_path()
    if not path.exists():
        if args.room_action=='status':print('No active team room.');return 0
        raise ValueError('No active team room.')
    if path.stat().st_mode&0o077:raise ValueError('Room runtime file must have private permissions.')
    data=json.loads(path.read_text());url=data['url'];parsed=urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.path:raise ValueError('Invalid local room control address.')
    action=args.room_action
    route='members' if action in ['status','members'] else action
    payload=json.dumps({'id':getattr(args,'id',None)}).encode() if action not in ['status','members'] else None
    req=urllib.request.Request(url+'/v1/'+route,data=payload,headers={'Authorization':'Bearer '+data['token'],'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=5) as response:result=json.load(response)
    except OSError:raise ValueError('Room is unreachable. If its process exited, remove only the stale private room.json receipt.') from None
    if args.room_action=='status' and not result.get('members'):
        print('Room active (joined member).')
    else:print(json.dumps(result,indent=2))
    return 0
