"""Opt-in shared albums. Only managed albums and contributions are mutated."""
import accounts
import immich


class Albums:
    def __init__(self, service, owner_id):
        self.service = service
        self.owner_id = owner_id
        if owner_id and owner_id not in service.accounts:
            raise ValueError('Album owner must be a configured account')

    @staticmethod
    def marker(pet_id):
        return f'Managed by Immich Pet Tagger\npet-id: {pet_id}'

    def owner(self, pet):
        owner_id = pet.get('album', {}).get('owner') or self.owner_id
        if not owner_id:
            raise ValueError('Shared albums are not enabled')
        if self.owner_id and owner_id != self.owner_id:
            raise ValueError('Album owner changed; migrate existing albums explicitly')
        return self.service.account(owner_id)

    def check(self, album, owner, pet_id):
        members = album.get('albumUsers', [])
        owners = [m['user']['id'] for m in members if m['role'] == 'owner']
        if owners != [owner.owner_id] or album.get('description') != self.marker(pet_id):
            raise ValueError('Refusing to modify an album without matching ownership and managed marker')

    def locate(self, pet_id, pet, owner):
        album_id = pet.get('album', {}).get('id')
        if album_id:
            r = owner.request('GET', f'albums/{album_id}', allow_not_found=True)
            if r.status_code != 404:
                album = r.json()
                self.check(album, owner, pet_id)
                return album
        matches = [a for a in owner.request('GET', 'albums', params={'isOwned': 'true'}).json()
                   if a.get('description') == self.marker(pet_id)]
        if len(matches) > 1:
            raise ValueError('Multiple managed albums found for this pet; resolve duplicates first')
        if matches:
            self.check(matches[0], owner, pet_id)
            return matches[0]
        return None

    def ensure(self, pet_id, pet, doc):
        owner = self.owner(pet)
        # Bind every owner before publishing anything; never infer identity from labels.
        contributors = [self.service.account(key) for key in self.service.accounts]
        expected = {a.owner_id for a in contributors}
        album = self.locate(pet_id, pet, owner)
        if album is None:
            album = owner.request('POST', 'albums', json={
                'albumName': pet['name'], 'description': self.marker(pet_id),
                'albumUsers': [{'userId': a.owner_id, 'role': 'editor'}
                               for a in contributors if a.id != owner.id],
            }).json()
            self.check(album, owner, pet_id)
        pet['album'] = {'id': album['id'], 'owner': owner.id}
        self.service.store.save(doc)  # Persist before subsequent writes; recover creation by marker.
        members = {m['user']['id']: m['role'] for m in album['albumUsers']}
        if set(members) - expected or album.get('hasSharedLink'):
            raise ValueError('Managed album has additional viewers or a public link; remove them before syncing')
        missing = expected - set(members)
        if missing:
            owner.request('PUT', f"albums/{album['id']}/users", json={
                'albumUsers': [{'userId': uid, 'role': 'editor'} for uid in sorted(missing)]})
        for uid, role in members.items():
            if uid != owner.owner_id and role != 'editor':
                owner.request('PUT', f"albums/{album['id']}/user/{uid}", json={'role': 'editor'})
        if album['albumName'] != pet['name']:
            owner.request('PATCH', f"albums/{album['id']}", json={'albumName': pet['name']})
        return owner, album['id'], contributors

    @staticmethod
    def check_bulk(response, ids, removing=False):
        result = response.json()
        by_id = {r.get('id'): r for r in result}
        for aid in ids:
            row = by_id.get(aid)
            if row is None or (not row.get('success') and row.get('error') != ('not_found' if removing else 'duplicate')):
                raise RuntimeError('Album asset operation failed; synchronization will retry')

    def sync(self, pet_id, pet, doc):
        owner, album_id, contributors = self.ensure(pet_id, pet, doc)
        excluded = {(r['account'], r['asset_id']) for r in doc['negatives']}
        errors = []
        for account in contributors:
            if self.service.cancel.is_set():
                return
            self.service.status['current_account'] = account.id
            try:
                desired = set()
                person = pet['persons'].get(account.id)
                if person:
                    # A removed person means its managed album contributions should disappear.
                    r = account.request('GET', f'people/{person}', allow_not_found=True)
                    if r.status_code != 404:
                        with accounts.using(account):
                            query = immich.search_params(person_id=person)
                            for page in immich._search_metadata_pages(query, 1000, 'shared pet album'):
                                if self.service.cancel.is_set():
                                    return  # Never remove after an interrupted/incomplete search.
                                for asset in page:
                                    aid = asset.get('id')
                                    if (aid and asset.get('ownerId') == account.owner_id
                                            and asset.get('visibility') != 'hidden'
                                            and not asset.get('isTrashed')
                                            and (account.id, aid) not in excluded):
                                        desired.add(aid)
                recorded = self.service.store.album_assets(album_id, account.id)
                # Re-add desired assets idempotently: also heals manually missing membership.
                for ids in self.chunks(desired):
                    if self.service.cancel.is_set():
                        return
                    # Journal intent before the network write, including uncertain/partial success.
                    self.service.store.record_album_assets(album_id, account.id, ids)
                    self.check_bulk(account.request('PUT', f'albums/{album_id}/assets', json={'ids': ids}), ids)
                for ids in self.chunks(recorded - desired):
                    if self.service.cancel.is_set():
                        return
                    self.check_bulk(owner.request('DELETE', f'albums/{album_id}/assets', json={'ids': ids}), ids, True)
                    self.service.store.forget_album_assets(album_id, account.id, ids)
            except (RuntimeError, ValueError) as exc:
                errors.append(f'{account.id}: {exc}')
        if errors:
            raise RuntimeError('; '.join(errors))

    @staticmethod
    def chunks(ids):
        values = sorted(ids)
        for start in range(0, len(values), 100):
            yield values[start:start + 100]

    def delete(self, pet_id, pet):
        if not self.owner_id and not pet.get('album'):
            return
        owner = self.owner(pet)
        album = self.locate(pet_id, pet, owner)
        if album:
            owner.request('DELETE', f"albums/{album['id']}", allow_not_found=True)
            self.service.store.clear_album_assets(album['id'])
        pet.pop('album', None)
