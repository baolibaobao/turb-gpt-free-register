# -*- coding: utf-8 -*-
import unittest

from core.otp_utils import looks_like_openai_email


class OtpUtilsTests(unittest.TestCase):
    def test_generic_verification_mail_is_not_treated_as_openai(self):
        self.assertFalse(looks_like_openai_email({
            "fromEmail": "notify@x.com",
            "subject": "617638 is your X verification code",
            "bodyPreview": "Your verification code is 617638",
        }))

    def test_generic_chinese_verification_mail_is_not_treated_as_openai(self):
        self.assertFalse(looks_like_openai_email({
            "fromEmail": "notify@example.com",
            "subject": "登录验证码",
            "bodyPreview": "您的验证码是 617638",
        }))

    def test_openai_sender_is_detected(self):
        self.assertTrue(looks_like_openai_email({
            "fromEmail": "noreply@tm.openai.com",
            "subject": "Your verification code",
            "bodyPreview": "Your code is 617638",
        }))


if __name__ == "__main__":
    unittest.main()
