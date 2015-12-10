# -*- coding: utf-8 -*-
import os
import sys
import logging
import socket
import json
import time
from user import User
from helpers import *
from config import config

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding, serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as padding_asym

logging.basicConfig()
logger = logging.getLogger('chat-server')
logger.setLevel(logging.DEBUG)

KEYCHAIN = {}
USERS = {}  # maps uid to User class
ATTEMPTED_LOGINS = {}


def load_server_keys():
    with open('server.private.key') as f:
        keystr = f.read()
        try:
            KEYCHAIN['private'] = parse_private_key(keystr)
        except Exception as e:
            logger.error("Couldn't read private key from file: {}".format(e))


def handle_login_init(body, sender):
    # send cookie
    cookie = make_cookie(sender)
    ATTEMPTED_LOGINS[cookie] = User(cookie, sender)
    resp = jdump({'kind': 'LOGIN', 'cookie': cookie, 'context': 'cookie', })
    logger.debug("RECV LOGIN INIT, RESP IS {}".format(resp))
    ATTEMPTED_LOGINS[cookie].send(_SOCK, resp)


def make_cookie(addr):
    dough = '-'.join(map(str, [addr[0], addr[1], time.time()]))
    # TODO: cookie = encrypt(dough)
    # return cookie
    return dough


def handle_login_submit(body, sender):
    logger.debug("RECV LOGIN SUBMIT from {}".format(sender))
    user = ATTEMPTED_LOGINS[body.get('cookie')]
    if not user:
        # DoS cookie is invalid
        logger.debug("terminating connection, cookie invalid in submit")
        terminate_login(body, sender)

    payload = jload(rsa_decrypt(KEYCHAIN['private'], pload(body['payload'])))
    user_public_key_bytes = rsa_decrypt(KEYCHAIN['private'],
                                        pload(body['public_key']))
    public_key = parse_public_key(user_public_key_bytes)

    k_session = aes_key().encode('base64')
    nonce_server = nonce(16)
    nonce_time = time.time()
    iv = init_vector(16)

    user.update_submit(payload['username'], payload['password_hash'],
                       payload['nonce_user'], public_key, k_session,
                       nonce_server, nonce_time, iv)

    # check user is registered and not logged in, with brown list handling
    # validate password hash

    resp = jdump({
        'kind': 'LOGIN',
        'context': 'challenge',
        'init_vector': iv.encode('base64'),
        'payload': pdump(user.challenge()),
    })

    user.send(_SOCK, resp)


def handle_login_response(body, sender):
    logger.debug("LOGIN RESPONSE RECEIVED")
    # validate the response to challenge
    user = ATTEMPTED_LOGINS[body.get('cookie')]
    if not user:
        # DoS cookie is invalid
        logger.debug("terminating connection, cookie invalid in response")
        terminate_login(body, sender)

    enc_answer = pload(body['payload'])
    # make sure this is wrapped in an error handler
    dec_answer = aes_decrypt(user.session_key.decode('base64'),
             user.iv, enc_answer)
    logger.debug("DECRYPTED ANSWER: {}".format(dec_answer))

    if dec_answer == user.nonce_server:
        logger.debug("LOGGING IN: {}".format(user.username))
        USERS[user.cookie] = user
    else:
        logger.debug("FAILED TO LOGIN: {}".format(user.username))
        logger.debug("ANSWER WAS: {}".format(enc_answer.decode('base64')))


login_handlers = {
    'INIT': handle_login_init,
    'SUBMIT': handle_login_submit,
    'RESPONSE': handle_login_response,
}


def handle_login(body, sender):
    ctx = body.get('context')
    if not ctx or ctx not in login_handlers:
        terminate_login(body, sender)

    handler = login_handlers[ctx]
    handler(body, sender)


def terminate_login(body, sender):
    # TODO: remove from ATTEMPTED_LOGINS
    # and penalize in brown list for failed attempt by IP
    pass


def get_cookie(hostname, addr):
    # TODO: make unique w/o username
    return "{}:{}".format(hostname, addr[1])


def handle_logout(*args):
    """
    ------
    User (U) to Server (S): ‘LOGOUT’ protocol:

    U -> S: Ksu{‘LOGOUT’, U, Nu}
    S -> U: Ksu{‘LOGOUT_ACK’, Nu}
    U -> S: Ksu{Nu}

    After logout, server will remove user from userlist, forgetting all temporary information,
    in particular the symmetric session key, the public key of the client, and the client's
    current network endpoint.

    """
    logger.debug("[RECEIVED LOGOUT]...")
    print args
    pass


def handle_list(body, sender):
    logger.debug("[RECEIVED LIST]...")

    user = USERS[body.get('cookie')]
    if not user:
        # DoS cookie is invalid
        logger.debug("terminating connection, cookie invalid in response")
        terminate_login(body, sender)

    # only ever one context, don't need to switch on context.
    payload = pload(body['payload'])

    data = jload(aes_decrypt(user.session_key.decode('base64'), user.iv, payload))

    #logger.debug("RECEIVED DATA: {}".format(data))

    if not data['username'] == user.username:
        logger.debug("INVALID LIST REQUEST -> USERNAME DOESN'T MATCH!")
        terminate_login(body, sender)
    
    list_response = jdump({
        'list': [u.username for u in USERS.values()],
        'nonce_user': data['nonce_user']
    })

    resp_payload = aes_encrypt(user.session_key.decode('base64'), user.iv, list_response)

    resp = jdump({
        'kind': 'LIST',
        'payload': pdump(resp_payload),
    })    

    logger.debug("SENDING LIST...")
    user.send(_SOCK, resp)


def handle_connect(*args):
    logger.debug("[RECEIVED CONNECT]...")
    print args

# a handler for each valid message type
handlers = {
    'LOGIN': handle_login,
    'LOGOUT': handle_logout,
    'LIST': handle_list,
    'CONNECT': handle_connect,
}


def listen_and_serve():
    logger.debug("starting server...")

    while True:
        raw_msg, sender = _SOCK.recvfrom(_PORT)
        try:
            msg = jload(raw_msg)
            assert msg.get('kind') in handlers.keys()
        except AssertionError:
            logger.error("Invalid message kind".format(msg['kind']))
        except Exception as e:
            logger.error("Unable to parse message: {}, e: {}".format(raw_msg,
                                                                     e))

        handler = handlers[msg.get('kind')]
        handler(msg, sender)


def network_init():
    global _PORT
    global _SOCK

    _PORT = config['server_port']

    _SOCK = socket.socket(type=socket.SOCK_DGRAM)
    _SOCK.bind(('', _PORT))

    logger.debug("server initialized on port {}".format(_PORT))


if __name__ == "__main__":
    load_server_keys()
    network_init()
    listen_and_serve()
