__author__ = 'chris'

import time
import json
import os.path
import nacl.signing
import nacl.hash
import nacl.encoding
import nacl.utils
import gnupg
import bitcoin
from dht.node import Node
from nacl.public import PrivateKey, PublicKey, Box
from twisted.internet import defer, reactor, task
from market.protocol import MarketProtocol
from dht.utils import digest
from constants import DATA_FOLDER
from protos import objects
from market.profile import Profile
from collections import OrderedDict
from binascii import hexlify, unhexlify
from keyutils.keys import KeyChain

class Server(object):
    def __init__(self, kserver, signing_key, database):
        """
        A high level class for sending direct, market messages to other nodes.
        A node will need one of these to participate in buying and selling.
        Should be initialized after the Kademlia server.
        """
        self.kserver = kserver
        self.signing_key = signing_key
        self.router = kserver.protocol.router
        self.db = database
        self.protocol = MarketProtocol(kserver.node.getProto(), self.router, signing_key, database)

    def get_contract(self, node_to_ask, contract_hash):
        """
        Will query the given node to fetch a contract given its hash.
        If the returned contract doesn't have the same hash, it will return None.

        After acquiring the contract it will download all the associated images if it
        does not already have them in cache.

        Args:
            node_to_ask: a `dht.node.Node` object containing an ip and port
            contract_hash: a 20 byte hash in raw byte format
        """

        def get_result(result):
            if digest(result[1][0]) == contract_hash:
                contract = json.loads(result[1][0], object_pairs_hook=OrderedDict)
                try:
                    signature = contract["vendor_offer"]["signature"]
                    pubkey = node_to_ask.signed_pubkey[64:]
                    verify_key = nacl.signing.VerifyKey(pubkey)
                    verify_key.verify(json.dumps(contract["vendor_offer"]["listing"], indent=4),
                                      unhexlify(signature))
                    for moderator in contract["vendor_offer"]["listing"]["moderators"]:
                        guid = moderator["guid"]
                        guid_key = moderator["pubkeys"]["signing"]["key"]
                        guid_sig = moderator["pubkeys"]["signing"]["signature"]
                        enc_key = moderator["pubkeys"]["encryption"]["key"]
                        enc_sig = moderator["pubkeys"]["encryption"]["signature"]
                        bitcoin_key = moderator["pubkeys"]["bitcoin"]["key"]
                        bitcoin_sig = moderator["pubkeys"]["bitcoin"]["signature"]
                        h = nacl.hash.sha512(unhexlify(guid_sig) + unhexlify(guid_key))
                        pow_hash = h[64:128]
                        if int(pow_hash[:6], 16) >= 50 or guid != h[:40]:
                            raise Exception('Invalid GUID')
                        verify_key = nacl.signing.VerifyKey(guid_key, encoder=nacl.encoding.HexEncoder)
                        verify_key.verify(unhexlify(enc_key), unhexlify(enc_sig))
                        verify_key.verify(unhexlify(bitcoin_key), unhexlify(bitcoin_sig))
                        # should probably also validate the handle here.
                except Exception:
                    return None
                self.cache(result[1][0])
                if "image_hashes" in contract["vendor_offer"]["listing"]["item"]:
                    for image_hash in contract["vendor_offer"]["listing"]["item"]["image_hashes"]:
                        self.get_image(node_to_ask, unhexlify(image_hash))
                return contract
            else:
                return None

        if node_to_ask.ip is None:
            return defer.succeed(None)
        d = self.protocol.callGetContract(node_to_ask, contract_hash)
        return d.addCallback(get_result)

    def get_image(self, node_to_ask, image_hash):
        """
        Will query the given node to fetch an image given its hash.
        If the returned image doesn't have the same hash, it will return None.

        Args:
            node_to_ask: a `dht.node.Node` object containing an ip and port
            image_hash: a 20 byte hash in raw byte format
        """

        def get_result(result):
            if digest(result[1][0]) == image_hash:
                self.cache(result[1][0])
                return result[1][0]
            else:
                return None

        if node_to_ask.ip is None:
            return defer.succeed(None)
        d = self.protocol.callGetImage(node_to_ask, image_hash)
        return d.addCallback(get_result)

    def get_profile(self, node_to_ask):
        """
        Downloads the profile from the given node. If the images do not already
        exist in cache, it will download and cache them before returning the profile.
        """

        def get_result(result):
            try:
                pubkey = node_to_ask.signed_pubkey[64:]
                verify_key = nacl.signing.VerifyKey(pubkey)
                verify_key.verify(result[1][1] + result[1][0])
                p = objects.Profile()
                p.ParseFromString(result[1][0])
                if p.pgp_key.public_key:
                    gpg = gnupg.GPG()
                    gpg.import_keys(p.pgp_key.publicKey)
                    if not gpg.verify(p.pgp_key.signature) or \
                                    node_to_ask.id.encode('hex') not in p.pgp_key.signature:
                        p.ClearField("pgp_key")
                if not os.path.isfile(DATA_FOLDER + 'cache/' + hexlify(p.avatar_hash)):
                    self.get_image(node_to_ask, p.avatar_hash)
                if not os.path.isfile(DATA_FOLDER + 'cache/' + hexlify(p.header_hash)):
                    self.get_image(node_to_ask, p.header_hash)
                return p
            except Exception:
                return None

        if node_to_ask.ip is None:
            return defer.succeed(None)
        d = self.protocol.callGetProfile(node_to_ask)
        return d.addCallback(get_result)

    def get_user_metadata(self, node_to_ask):
        """
        Downloads just a small portion of the profile (containing the name, handle,
        and avatar hash). We need this for some parts of the UI where we list stores.
        Since we need fast loading we shouldn't download the full profile here.
        It will download the avatar if it isn't already in cache.
        """

        def get_result(result):
            try:
                pubkey = node_to_ask.signed_pubkey[64:]
                verify_key = nacl.signing.VerifyKey(pubkey)
                verify_key.verify(result[1][1] + result[1][0])
                m = objects.Metadata()
                m.ParseFromString(result[1][0])
                if not os.path.isfile(DATA_FOLDER + 'cache/' + hexlify(m.avatar_hash)):
                    self.get_image(node_to_ask, m.avatar_hash)
                return m
            except Exception:
                return None

        if node_to_ask.ip is None:
            return defer.succeed(None)
        d = self.protocol.callGetUserMetadata(node_to_ask)
        return d.addCallback(get_result)

    def get_listings(self, node_to_ask):
        """
        Queries a store for it's list of contracts. A `objects.Listings` protobuf
        is returned containing some metadata for each contract. The individual contracts
        should be fetched with a get_contract call.
        """

        def get_result(result):
            try:
                pubkey = node_to_ask.signed_pubkey[64:]
                verify_key = nacl.signing.VerifyKey(pubkey)
                verify_key.verify(result[1][1] + result[1][0])
                l = objects.Listings()
                l.ParseFromString(result[1][0])
                return l
            except Exception:
                return None

        if node_to_ask.ip is None:
            return defer.succeed(None)
        d = self.protocol.callGetListings(node_to_ask)
        return d.addCallback(get_result)

    def get_contract_metadata(self, node_to_ask, contract_hash):
        """
        Downloads just the metadata for the contract. Useful for displaying
        search results in a list view without downloading the entire contract.
        It will download the thumbnail image if it isn't already in cache.
        """

        def get_result(result):
            try:
                pubkey = node_to_ask.signed_pubkey[64:]
                verify_key = nacl.signing.VerifyKey(pubkey)
                verify_key.verify(result[1][1] + result[1][0])
                l = objects.Listings().ListingMetadata()
                l.ParseFromString(result[1][0])
                if l.HasField("thumbnail_hash"):
                    if not os.path.isfile(DATA_FOLDER + 'cache/' + hexlify(l.thumbnail_hash)):
                        self.get_image(node_to_ask, l.thumbnail_hash)
                return l
            except Exception:
                return None

        if node_to_ask.ip is None:
            return defer.succeed(None)
        d = self.protocol.callGetContractMetadata(node_to_ask, contract_hash)
        return d.addCallback(get_result)

    def make_moderator(self):
        """
        Set self as a moderator in the DHT.
        """

        u = objects.Profile()
        k = u.PublicKey()
        k.public_key = bitcoin.bip32_deserialize(KeyChain(self.db).bitcoin_master_pubkey)[5]
        k.signature = self.signing_key.sign(k.public_key)[:64]
        u.bitcoin_key.MergeFrom(k)
        u.moderator = True
        Profile(self.db).update(u)
        proto = self.kserver.node.getProto().SerializeToString()
        self.kserver.set(digest("moderators"), digest(proto), proto)

    def unmake_moderator(self):
        """
        Deletes our moderator entry from the network.
        """

        key = digest(self.kserver.node.getProto().SerializeToString())
        signature = self.signing_key.sign(key)[:64]
        self.kserver.delete("moderators", key, signature)
        Profile(self.db).remove_field("moderator")

    def follow(self, node_to_follow):
        """
        Sends a follow message to another node in the network. The node must be online
        to receive the message. The message contains a signed, serialized `Follower`
        protobuf object which the recipient will store and can send to other nodes,
        proving you are following them. The response is a signed `Metadata` protobuf
        that will store in the db.
        """

        def save_to_db(result):
            if result[0] and result[1][0] == "True":
                try:
                    u = objects.Following.User()
                    u.guid = node_to_follow.id
                    u.signed_pubkey = node_to_follow.signed_pubkey
                    m = objects.Metadata()
                    m.ParseFromString(result[1][1])
                    u.metadata.MergeFrom(m)
                    u.signature = result[1][2]
                    pubkey = node_to_follow.signed_pubkey[64:]
                    verify_key = nacl.signing.VerifyKey(pubkey)
                    verify_key.verify(result[1][1], result[1][2])
                    self.db.FollowData().follow(u)
                    return True
                except Exception:
                    return False
            else:
                return False

        proto = Profile(self.db).get(False)
        m = objects.Metadata()
        m.name = proto.name
        m.handle = proto.handle
        m.avatar_hash = proto.avatar_hash
        m.nsfw = proto.nsfw
        f = objects.Followers.Follower()
        f.guid = self.kserver.node.id
        f.following = node_to_follow.id
        f.signed_pubkey = self.kserver.node.signed_pubkey
        f.metadata.MergeFrom(m)
        signature = self.signing_key.sign(f.SerializeToString())[:64]
        d = self.protocol.callFollow(node_to_follow, f.SerializeToString(), signature)
        return d.addCallback(save_to_db)

    def unfollow(self, node_to_unfollow):
        """
        Sends an unfollow message to a node and removes them from our db.
        """

        def save_to_db(result):
            if result[0] and result[1][0] == "True":
                self.db.FollowData().unfollow(node_to_unfollow.id)
                return True
            else:
                return False

        signature = self.signing_key.sign("unfollow:" + node_to_unfollow.id)[:64]
        d = self.protocol.callUnfollow(node_to_unfollow, signature)
        return d.addCallback(save_to_db)

    def get_followers(self, node_to_ask):
        """
        Query the given node for a list if its followers. The response will be a
        `Followers` protobuf object. We will verify the signature for each follower
        to make sure that node really did follower this user.
        """

        def get_response(response):
            # Verify the signature on the response
            f = objects.Followers()
            try:
                pubkey = node_to_ask.signed_pubkey[64:]
                verify_key = nacl.signing.VerifyKey(pubkey)
                verify_key.verify(response[1][1] + response[1][0])
                f.ParseFromString(response[1][0])
            except Exception:
                return None
            # Verify the signature and guid of each follower.
            for follower in f.followers:
                try:
                    v_key = nacl.signing.VerifyKey(follower.signed_pubkey[64:])
                    signature = follower.signature
                    follower.ClearField("signature")
                    v_key.verify(follower.SerializeToString(), signature)
                    h = nacl.hash.sha512(follower.signed_pubkey)
                    pow_hash = h[64:128]
                    if int(pow_hash[:6], 16) >= 50 or hexlify(follower.guid) != h[:40]:
                        raise Exception('Invalid GUID')
                    if follower.following != node_to_ask.id:
                        raise Exception('Invalid follower')
                except Exception:
                    f.followers.remove(follower)
            return f

        d = self.protocol.callGetFollowers(node_to_ask)
        return d.addCallback(get_response)

    def get_following(self, node_to_ask):
        """
        Query the given node for a list of users it's following. The return
        is `Following` protobuf object that contains signed metadata for each
        user this node is following. The signature on the metadata is there to
        prevent this node from altering the name/handle/avatar associated with
        the guid.
        """

        def get_response(response):
            # Verify the signature on the response
            f = objects.Following()
            try:
                pubkey = node_to_ask.signed_pubkey[64:]
                verify_key = nacl.signing.VerifyKey(pubkey)
                verify_key.verify(response[1][1] + response[1][0])
                f.ParseFromString(response[1][0])
            except Exception:
                return None
            for user in f.users:
                try:
                    v_key = nacl.signing.VerifyKey(user.signed_pubkey[64:])
                    signature = user.signature
                    v_key.verify(user.metadata.SerializeToString(), signature)
                    h = nacl.hash.sha512(user.signed_pubkey)
                    pow_hash = h[64:128]
                    if int(pow_hash[:6], 16) >= 50 or hexlify(user.guid) != h[:40]:
                        raise Exception('Invalid GUID')
                except Exception:
                    f.users.remove(user)
            return f

        d = self.protocol.callGetFollowing(node_to_ask)
        return d.addCallback(get_response)

    def send_notification(self, message):
        """
        Sends a notification message to all online followers. It will resolve
        each guid before sending the notification. Messages must be less than
        140 characters. Returns the number of followers the notification reached.
        """

        if len(message) > 140:
            return defer.succeed(0)

        def send(nodes):
            def how_many_reached(responses):
                count = 0
                for resp in responses:
                    if resp[1][0] and resp[1][1][0] == "True":
                        count += 1
                return count

            ds = []
            signature = self.signing_key.sign(str(message))[:64]
            for n in nodes:
                if n[1] is not None:
                    ds.append(self.protocol.callNotify(n[1], message, signature))
            return defer.DeferredList(ds).addCallback(how_many_reached)
        dl = []
        f = objects.Followers()
        f.ParseFromString(self.db.FollowData().get_followers())
        for follower in f.followers:
            dl.append(self.kserver.resolve(follower.guid))
        return defer.DeferredList(dl).addCallback(send)

    def send_message(self, receiving_node, public_key, message_type, message, subject=None, store_only=False):
        """
        Sends a message to another node. If the node isn't online it
        will be placed in the dht for the node to pick up later.
        """
        pro = Profile(self.db).get()
        if len(message) > 1500:
            return
        p = objects.Plaintext_Message()
        p.sender_guid = self.kserver.node.id
        p.signed_pubkey = self.kserver.node.signed_pubkey
        p.encryption_pubkey = PrivateKey(self.signing_key.encode()).public_key.encode()
        p.type = message_type
        p.message = message
        if subject is not None:
            p.subject = subject
        if pro.handle:
            p.handle = pro.handle
        if pro.avatar_hash:
            p.avatar_hash = pro.avatar_hash
        p.timestamp = int(time.time())
        signature = self.signing_key.sign(p.SerializeToString())[:64]
        p.signature = signature

        skephem = PrivateKey.generate()
        pkephem = skephem.public_key.encode(nacl.encoding.RawEncoder)
        box = Box(skephem, PublicKey(public_key, nacl.encoding.HexEncoder))
        nonce = nacl.utils.random(Box.NONCE_SIZE)
        ciphertext = box.encrypt(p.SerializeToString(), nonce)

        def get_response(response):
            if not response[0]:
                self.kserver.set(digest(receiving_node.id), pkephem, ciphertext)
        if not store_only:
            self.protocol.callMessage(receiving_node, pkephem, ciphertext).addCallback(get_response)
        else:
            get_response([False])

    def get_messages(self, listener):
        # if the transport hasn't been initialized yet, wait a second
        if self.protocol.multiplexer is None or self.protocol.multiplexer.transport is None:
            return task.deferLater(reactor, 1, self.get_messages, listener)

        def parse_messages(messages):
            if messages is not None:
                for message in messages:
                    try:
                        value = objects.Value()
                        value.ParseFromString(message)
                        try:
                            box = Box(PrivateKey(self.signing_key.encode()), PublicKey(value.valueKey))
                            ciphertext = value.serializedData
                            plaintext = box.decrypt(ciphertext)
                            p = objects.Plaintext_Message()
                            p.ParseFromString(plaintext)
                            signature = p.signature
                            p.ClearField("signature")
                            verify_key = nacl.signing.VerifyKey(p.signed_pubkey[64:])
                            verify_key.verify(p.SerializeToString(), signature)
                            h = nacl.hash.sha512(p.signed_pubkey)
                            pow_hash = h[64:128]
                            if int(pow_hash[:6], 16) >= 50 or hexlify(p.sender_guid) != h[:40]:
                                raise Exception('Invalid guid')
                            listener.notify(p.sender_guid, p.encryption_pubkey, p.subject,
                                            objects.Plaintext_Message.Type.Name(p.type), p.message)
                        except Exception:
                            pass
                        signature = self.signing_key.sign(value.valueKey)[:64]
                        self.kserver.delete(self.kserver.node.id, value.valueKey, signature)
                    except Exception:
                        pass
        self.kserver.get(self.kserver.node.id).addCallback(parse_messages)

    def purchase(self, node_to_ask, contract):
        """
        Send an order message to the vendor.

        Args:
            node_to_ask: a `dht.node.Node` object
            contract: a complete `Contract` object containing the buyer's order
        """

        def parse_response(response):
            try:
                address = contract.contract["buyer_order"]["order"]["payment"]["address"]
                verify_key = nacl.signing.VerifyKey(node_to_ask.signed_pubkey[64:])
                verify_key.verify(str(address), response[1][0])
                return response[1][0]
            except Exception:
                return False

        public_key = contract.contract["vendor_offer"]["listing"]["id"]["pubkeys"]["encryption"]
        skephem = PrivateKey.generate()
        pkephem = skephem.public_key.encode(nacl.encoding.RawEncoder)
        box = Box(skephem, PublicKey(public_key, nacl.encoding.HexEncoder))
        nonce = nacl.utils.random(Box.NONCE_SIZE)
        ciphertext = box.encrypt(json.dumps(contract.contract, indent=4), nonce)
        d = self.protocol.callOrder(node_to_ask, pkephem, ciphertext)
        return d.addCallback(parse_response)

    def confirm_order(self, guid, contract):
        """
        Send the order confirmation over to the buyer. If the buyer isn't
        online we will stick it in the DHT temporarily.
        """

        def get_node(node_to_ask):
            def parse_response(response):
                if response[0] and response[1][0] == "True":
                    return True
                elif not response[0]:
                    contract_dict = json.loads(json.dumps(contract.contract, indent=4),
                                               object_pairs_hook=OrderedDict)
                    del contract_dict["vendor_order_confirmation"]
                    order_id = digest(json.dumps(contract_dict, indent=4)).encode("hex")
                    self.send_message(Node(guid),
                                      contract.contract["buyer_order"]["order"]["id"]["pubkeys"]["encryption"],
                                      objects.Plaintext_Message.Type.Value("ORDER"),
                                      json.dumps(contract.contract["vendor_order_confirmation"]),
                                      order_id,
                                      store_only=True)
                    return True
                else:
                    return False

            if node_to_ask:
                public_key = contract.contract["buyer_order"]["order"]["id"]["pubkeys"]["encryption"]
                skephem = PrivateKey.generate()
                pkephem = skephem.public_key.encode(nacl.encoding.RawEncoder)
                box = Box(skephem, PublicKey(public_key, nacl.encoding.HexEncoder))
                nonce = nacl.utils.random(Box.NONCE_SIZE)
                ciphertext = box.encrypt(json.dumps(contract.contract, indent=4), nonce)
                self.protocol.callOrderConfirmation(node_to_ask, pkephem, ciphertext).addCallback(parse_response)
            else:
                parse_response([False])
        return self.kserver.resolve(unhexlify(guid)).addCallback(get_node)

    @staticmethod
    def cache(filename):
        """
        Saves the file to a cache folder if it doesn't already exist.
        """
        if not os.path.isfile(DATA_FOLDER + "cache/" + digest(filename).encode("hex")):
            with open(DATA_FOLDER + "cache/" + digest(filename).encode("hex"), 'w') as outfile:
                outfile.write(filename)
