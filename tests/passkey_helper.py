"""Software authenticator for real WebAuthn signature verification tests."""

import hashlib
import json
import secrets

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url


class SoftwarePasskey:
    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.user_handle = b""

    def response(
        self,
        options,
        *,
        registration=False,
        origin="http://localhost:8000",
        uv=True,
        counter=1,
        challenge=None,
        user_handle=None,
        cross_origin=False,
    ):
        client_data = json.dumps(
            {
                "type": "webauthn.create" if registration else "webauthn.get",
                "challenge": challenge or options["challenge"],
                "origin": origin,
                "crossOrigin": cross_origin,
            }
        ).encode()
        rp_id = options["rp"]["id"] if registration else options["rpId"]
        flags = 1 | (4 if uv else 0) | (64 if registration else 0)
        auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags])
        auth_data += (0 if registration else counter).to_bytes(4, "big")
        response = {"clientDataJSON": bytes_to_base64url(client_data)}
        if registration:
            self.user_handle = base64url_to_bytes(options["user"]["id"])
            public = self.key.public_key().public_numbers()
            cose = cbor2.dumps(
                {
                    1: 2,
                    3: -7,
                    -1: 1,
                    -2: public.x.to_bytes(32, "big"),
                    -3: public.y.to_bytes(32, "big"),
                }
            )
            auth_data += (
                bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cose
            )
            response["attestationObject"] = bytes_to_base64url(
                cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
            )
        else:
            response["authenticatorData"] = bytes_to_base64url(auth_data)
            response["signature"] = bytes_to_base64url(
                self.key.sign(
                    auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
                )
            )
            response["userHandle"] = bytes_to_base64url(
                self.user_handle if user_handle is None else user_handle
            )
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": response,
        }
