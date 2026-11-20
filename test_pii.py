import unittest

import greendata as gd


def with_check_digit(first11):
    """Build a valid 12 digit Verhoeff number, like a real Aadhaar."""
    for d in "0123456789":
        if gd.verhoeff_ok(first11 + d):
            return first11 + d
    raise AssertionError


AADHAAR = with_check_digit("23456789012")
SPACED = f"{AADHAAR[:4]} {AADHAAR[4:8]} {AADHAAR[8:]}"


class PiiTests(unittest.TestCase):
    def test_phone_is_found_with_context(self):
        for t in ("mobile: 9876543210", "Call me on +91 98765 43210", "contact 9876543210", "98765-43210",
                  "name,email,phone\nraj,raj,9876543210\n", "whatsapp no 9876543210"):
            self.assertIn("phone", gd.has_pii(t), t)

    def test_plain_list_of_numbers_is_protected(self):
        self.assertIn("phone", gd.has_pii("\n".join(str(9000000000 + i * 7919) for i in range(6))))

    def test_ids_are_not_phones(self):
        for t in ("order 7123456789 shipped", "invoice 9876543210", "Tracking: 8123456789", "txn 6123456789", "ref no 9123456789"):
            self.assertNotIn("phone", gd.has_pii(t), t)

    def test_tracking_numbers_starting_with_91_are_not_phones(self):
        self.assertNotIn("phone", gd.has_pii("order 1610536048 tracking 916425945414\n"))
        self.assertIn("phone", gd.has_pii("whatsapp +919876543210"))
        self.assertIn("phone", gd.has_pii("tel 91 9876543210"))

    def test_single_bare_number_is_not_enough(self):
        self.assertEqual(gd.has_pii("the total was 9876543210 units"), [])

    def test_aadhaar(self):
        self.assertIn("aadhaar", gd.has_pii(f"id {SPACED}"))                    # 4-4-4 format
        self.assertIn("aadhaar", gd.has_pii(f"Aadhaar number {AADHAAR}"))       # plain digits with the word
        self.assertNotIn("aadhaar", gd.has_pii(f"shipment {AADHAAR}"))          # plain digits, no context
        self.assertNotIn("aadhaar", gd.has_pii(f"tracking {AADHAAR}"))
        bad = AADHAAR[:-1] + str((int(AADHAAR[-1]) + 1) % 10)
        self.assertNotIn("aadhaar", gd.has_pii(f"Aadhaar {bad}"))              # fails the checksum

    def test_pan_needs_a_real_holder_letter(self):
        self.assertIn("PAN", gd.has_pii("PAN ABCPE1234F"))
        self.assertNotIn("PAN", gd.has_pii("code ABCXE1234F"))

    def test_email(self):
        self.assertEqual(gd.has_pii("write to a.b@example.co.in"), ["email"])
        self.assertEqual(gd.has_pii("nothing here"), [])


if __name__ == "__main__":
    unittest.main()
