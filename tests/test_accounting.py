import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import accounting as a
import chef


class Payroll(unittest.TestCase):
    def test_tarif_grenzen(self):
        self.assertEqual(a._tarif(12348), 0)
        self.assertEqual(a._tarif(17799), 1034)

    def test_minijob(self):
        c = a.calc_payroll(60000, dict(type="minijob"))
        self.assertEqual(c["lohnsteuer"], 0)
        self.assertEqual(c["netto"], 60000 - 2160)      # nur 3,6 % RV
        self.assertEqual(c["ag_pausch"], 18000)          # 30 % Pauschalabgaben

    def test_minijob_ueber_grenze_wird_midijob(self):
        self.assertTrue(a.calc_payroll(90000, dict(type="minijob"))["kind"].startswith("Midijob"))

    def test_netto_kleiner_brutto_und_summe(self):
        c = a.calc_payroll(280000, dict(tax_class=1))
        self.assertEqual(c["netto"], c["brutto"] - c["abzuege"])
        self.assertTrue(180000 < c["netto"] < 220000)
        self.assertEqual(c["rv"], round(280000 * 0.093))

    def test_klasse3_weniger_steuer_als_klasse1(self):
        self.assertLess(a.calc_payroll(400000, dict(tax_class=3))["lohnsteuer"], a.calc_payroll(400000, dict(tax_class=1))["lohnsteuer"])

    def test_kirchensteuer(self):
        self.assertGreater(a.calc_payroll(400000, dict(church=1))["kist"], 0)
        self.assertEqual(a.calc_payroll(400000, {})["kist"], 0)


class Rechnungen(unittest.TestCase):
    def setUp(self):
        self.db = chef.connect(":memory:")

    def test_steuer_je_satz_und_nummernkreis(self):
        r = a.create_invoice(self.db, "out", "Kunde", [{"description": "Essen", "qty": 10, "unit_cents": 2000, "tax_rate": 7},
                                                       {"description": "Wein", "qty": 2, "unit_cents": 1500, "tax_rate": 19}])
        self.assertEqual((r["net_cents"], r["tax_cents"]), (23000, 1400 + 570))
        r2 = a.create_invoice(self.db, "out", "Kunde 2", [{"description": "x", "qty": 1, "unit_cents": 100, "tax_rate": 0}])
        self.assertTrue(r["number"].endswith("0001") and r2["number"].endswith("0002"))

    def test_validierung(self):
        for bad in ([], [{"description": "x", "qty": 0, "unit_cents": 1, "tax_rate": 19}],
                    [{"description": "x", "qty": 1, "unit_cents": 1, "tax_rate": 5}]):
            with self.assertRaises(ValueError):
                a.create_invoice(self.db, "out", "K", bad)

    def test_zahllast_und_storno(self):
        i = a.create_invoice(self.db, "out", "K", [{"description": "x", "qty": 1, "unit_cents": 10000, "tax_rate": 19}])
        a.create_invoice(self.db, "in", "L", [{"description": "y", "qty": 1, "unit_cents": 5000, "tax_rate": 19}], ext_number="1")
        m = a.date.today().strftime("%Y-%m")
        self.assertEqual(a.summary(self.db, m)["zahllast_cents"], 1900 - 950)
        a.set_invoice_status(self.db, i["id"], "storniert")
        self.assertEqual(a.summary(self.db, m)["ust_cents"], 0)


class Integration(unittest.TestCase):
    def test_demo_und_agent(self):
        db = chef.connect(":memory:")
        chef.seed_demo(db)
        self.assertIn("Netto", chef.ask_agent(db, "Wie hoch ist das Netto von Mehdi?")["answer"])
        self.assertIn("Offen", chef.ask_agent(db, "Welche Rechnungen sind offen?")["answer"])
        self.assertTrue(a.export_csv(db, a.date.today().strftime("%Y-%m")).startswith("Umsatz;"))


if __name__ == "__main__":
    unittest.main()
