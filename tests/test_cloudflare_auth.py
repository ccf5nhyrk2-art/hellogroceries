import os
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
import cloudflare_auth as cf

class CloudflareJWTTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cls.pub=type("PublicKey",(),{"key":cls.key.public_key()})()

    def token(self, **changes):
        payload={"iss":cf.ISSUER,"aud":cf.AUD,"sub":"user-id","email":"admin@example.com","iat":int(time.time()),"exp":int(time.time())+300}
        payload.update(changes)
        return jwt.encode(payload,self.key,algorithm="RS256")

    def verify(self, token):
        with patch.object(cf.KEYS,"get_signing_key_from_jwt",return_value=self.pub):
            return cf.verified_email(token)

    def test_valid(self):
        self.assertEqual(self.verify(self.token()),"admin@example.com")

    def test_missing_or_forged(self):
        self.assertIsNone(self.verify(""))
        self.assertIsNone(self.verify("forged"))

    def test_wrong_audience(self):
        self.assertIsNone(self.verify(self.token(aud="wrong")))

    def test_wrong_issuer(self):
        self.assertIsNone(self.verify(self.token(iss="https://evil.example")))

    def test_expired(self):
        self.assertIsNone(self.verify(self.token(exp=int(time.time())-120)))

    def test_missing_identity(self):
        self.assertIsNone(self.verify(self.token(email=None)))

if __name__=="__main__":
    unittest.main()
