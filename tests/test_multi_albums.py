"""Album isolation, lifecycle, and retry behavior; no live credentials."""
from uuid import uuid4
import pytest
import accounts
from multi_albums import Albums
from tests.test_multi_account import setup_service, Reply

@pytest.fixture
def albums(setup_service, monkeypatch):
    service,fake,pet,ref,*_=setup_service
    service.albums=Albums(service,'shared')
    original=fake.request
    remote={};calls=[];failures=set()
    def request(method,url,headers=None,json=None,params=None,**kw):
        path=url.split('/api/')[-1];account=(headers or {}).get('x-api-key','').removeprefix('secret-')
        if path=='search/metadata' and 'personIds' in (json or {}).get('filter',{}):
            person=json['filter']['personIds']['all'][0]
            items=[a for a in fake.assets.values() if any(f['person']['id']==person for f in fake.faces.get(a['id'],[]))]
            return Reply({'assets':{'items':items,'nextCursor':None}})
        if not path.startswith('albums'):
            return original(method,url,headers=headers,json=json,params=params,**kw)
        calls.append((account,method,path,json))
        if (method,path) in failures:raise RuntimeError('Simulated album failure')
        if path=='albums':
            if method=='GET':return Reply([a for a in remote.values() if a['albumUsers'][0]['user']['id']=='owner-'+account])
            aid=str(uuid4())
            remote[aid]={'id':aid,'albumName':json['albumName'],'description':json['description'],'albumUsers':[{'user':{'id':'owner-'+account},'role':'owner'}]+[{'user':{'id':u['userId']},'role':u['role']} for u in json['albumUsers']],'hasSharedLink':False,'assets':set()}
            return Reply(remote[aid],201)
        parts=path.split('/');aid=parts[1]
        if aid not in remote:return Reply({},404)
        a=remote[aid]
        if len(parts)==2:
            if method=='DELETE':
                assert a['albumUsers'][0]['user']['id']=='owner-'+account
                del remote[aid];return Reply({},204)
            if method=='PATCH':a.update(json)
            return Reply(a)
        if parts[2]=='assets':
            result=[]
            for asset in json['ids']:
                if method=='PUT':
                    assert fake.assets[asset]['ownerId']=='owner-'+account,'CROSS-OWNER CONTRIBUTION'
                    a['assets'].add(asset)
                else:
                    assert a['albumUsers'][0]['user']['id']=='owner-'+account
                    a['assets'].discard(asset)
                result.append({'id':asset,'success':True})
            return Reply(result)
        if parts[2]=='users':
            a['albumUsers'].extend({'user':{'id':u['userId']},'role':u['role']} for u in json['albumUsers']);return Reply(a)
        if parts[2]=='user':
            next(u for u in a['albumUsers'] if u['user']['id']==parts[3])['role']=json['role'];return Reply({},204)
        raise AssertionError(path)
    monkeypatch.setattr(fake,'request',request);monkeypatch.setattr(accounts.requests,'request',request)
    return service,fake,pet,ref,remote,calls,failures

def test_multiple_pets_all_owners_and_lifecycle(albums):
    s,f,p,r,remote,calls,_=albums
    f.add_asset('user_b');f.add_asset('shared');s.scan()
    aid=s.store.enrollment()['pets'][p]['album']['id']
    assert remote[aid]['assets']==set(f.assets)
    assert len(remote[aid]['albumUsers'])==3
    second=s.create_pet('Pippin');assert len(remote)==2
    s.update_pet(p,{'name':'Mochi updated'});assert remote[aid]['albumName']=='Mochi updated'
    s.delete_pet(second,local_only=True);assert len(remote)==1
    s.delete_pet(p);assert not remote and len(f.assets)==3
    assert not any(m=='DELETE' and path.startswith('assets/') for _,m,path,_ in calls)

def test_album_only_backfill_without_models(albums,monkeypatch):
    s,f,p,r,remote,_,_=albums
    s.albums=Albums(s,None);s.scan();s.albums=Albums(s,'shared')
    monkeypatch.setattr(s,'train',lambda *_:pytest.fail('No inference for album backfill'))
    s.scan(albums_only=True);assert next(iter(remote.values()))['assets']=={r}

def test_negative_and_removed_face_preserve_manual_contributions(albums):
    s,f,p,r,remote,_,_=albums
    s.scan();a=next(iter(remote.values()));manual=f.add_asset('shared');a['assets'].add(manual)
    s.add_reference('user_a',r);s.scan(albums_only=True);assert a['assets']=={manual}
    s.remove_reference('user_a',r);s.scan(albums_only=True);assert r in a['assets']
    f.faces[r]=[];s.scan(albums_only=True);assert a['assets']=={manual}

def test_marker_recovery_does_not_adopt_same_name_album(albums):
    s,_,p,_,remote,_,_=albums
    d=s.store.enrollment();s.sync_albums(d);aid=d['pets'][p]['album']['id']
    d['pets'][p].pop('album');s.store.save(d)
    remote['unrelated']={'id':'unrelated','albumName':'Mochi','description':'Personal','albumUsers':[{'user':{'id':'owner-shared'},'role':'owner'}]}
    s.sync_albums(d);assert d['pets'][p]['album']['id']==aid and len(remote)==2
    s.delete_pet(p,local_only=True);assert set(remote)=={'unrelated'}

def test_failed_delete_retries_without_recreation(albums):
    s,_,p,_,remote,_,failures=albums
    d=s.store.enrollment();s.sync_albums(d);aid=d['pets'][p]['album']['id'];failures.add(('DELETE','albums/'+aid))
    with pytest.raises(RuntimeError):s.delete_pet(p,local_only=True)
    s.scan(albums_only=True);assert len(remote)==1 and s.store.enrollment()['pets'][p]['deleting']
    failures.clear();s.scan(albums_only=True);assert not remote and p not in s.store.enrollment()['pets']

def test_extra_viewer_blocks_publication(albums):
    s,_,p,_,remote,_,_=albums
    d=s.store.enrollment();s.sync_albums(d);a=next(iter(remote.values()))
    a['albumUsers'].append({'user':{'id':'unknown-viewer'},'role':'viewer'})
    s.scan();assert not a['assets'];assert 'additional viewers' in s.store.enrollment()['pets'][p]['album_error']

def test_failed_add_retries_without_reinference(albums):
    s,_,p,r,remote,_,failures=albums
    d=s.store.enrollment();s.sync_albums(d);aid=d['pets'][p]['album']['id'];failures.add(('PUT',f'albums/{aid}/assets'))
    s.scan();assert not remote[aid]['assets'];assert s.store.enrollment()['pets'][p]['album_error']
    failures.clear();s.scan(albums_only=True);assert remote[aid]['assets']=={r}

def test_inaccessible_owner_does_not_remove_contributions(albums):
    s,f,_,r,remote,_,_=albums
    s.scan();a=next(iter(remote.values()));f.disabled.add('user_a');s.scan(albums_only=True);assert a['assets']=={r}

def test_bulk_failure_not_silently_accepted():
    with pytest.raises(RuntimeError):Albums.check_bulk(Reply([{'id':'asset','success':False,'error':'no_permission'}]),['asset'])
    with pytest.raises(RuntimeError):Albums.check_bulk(Reply([]),['asset'])
    Albums.check_bulk(Reply([{'id':'asset','success':False,'error':'duplicate'}]),['asset'])

def test_two_pets_have_separate_membership(albums):
    s,f,p,r,remote,_,_=albums
    s.scan();p2=s.create_pet('Pippin');r2=f.add_asset('user_b')
    d=s.store.enrollment();a=s.account('user_b')
    person=a.request('POST','people',json={'name':'Pippin'}).json()['id']
    d['pets'][p2]['persons']['user_b']=person;s.store.save(d)
    f.faces[r2]=[{'person':{'id':person}}];s.scan(albums_only=True)
    d=s.store.enrollment()
    assert remote[d['pets'][p]['album']['id']]['assets']=={r}
    assert remote[d['pets'][p2]['album']['id']]['assets']=={r2}

def test_missing_album_recreated_with_matches(albums):
    s,_,p,r,remote,_,_=albums
    s.scan();old=next(iter(remote));del remote[old]
    s.scan(albums_only=True)
    assert len(remote)==1 and old not in remote
    assert next(iter(remote.values()))['assets']=={r}

def test_partial_search_failure_does_not_remove_managed_assets(albums,monkeypatch):
    import immich
    s,_,_,r,remote,_,_=albums
    s.scan();a=next(iter(remote.values()))
    def failed(*args):
        yield []
        raise RuntimeError('Second page failed')
    monkeypatch.setattr(immich,'_search_metadata_pages',failed)
    s.scan(albums_only=True)
    assert a['assets']=={r}

def test_already_removed_membership_is_idempotent():
    Albums.check_bulk(Reply([{'id':'asset','success':False,'error':'not_found'}]),['asset'],removing=True)
    with pytest.raises(RuntimeError):
        Albums.check_bulk(Reply([{'id':'asset','success':False,'error':'not_found'}]),['asset'])

def test_removal_after_ambiguous_add_uses_journal(albums,monkeypatch):
    s,f,p,r,remote,_,_=albums
    original=f.request
    def uncertain(method,url,**kw):
        result=original(method,url,**kw)
        if method=='PUT' and url.endswith('/assets'):raise RuntimeError('Connection lost after write')
        return result
    monkeypatch.setattr(f,'request',uncertain);monkeypatch.setattr(accounts.requests,'request',uncertain)
    s.scan();a=next(iter(remote.values()));assert a['assets']=={r}
    monkeypatch.setattr(f,'request',original);monkeypatch.setattr(accounts.requests,'request',original)
    s.add_reference('user_a',r);s.scan(albums_only=True);assert not a['assets']

def test_partner_results_cannot_be_contributed_by_wrong_key(albums):
    s,f,p,r,remote,_,_=albums
    s.scan();foreign=f.add_asset('user_b')
    person=s.store.enrollment()['pets'][p]['persons']['user_a']
    f.faces[foreign]=[{'person':{'id':person}}]
    s.scan(albums_only=True)
    assert next(iter(remote.values()))['assets']=={r}

def test_modified_album_marker_prevents_deletion(albums):
    s,_,p,_,remote,_,_=albums
    s.scan();a=next(iter(remote.values()));a['description']='Different purpose'
    with pytest.raises(ValueError):s.delete_pet(p,local_only=True)
    assert a['id'] in remote
