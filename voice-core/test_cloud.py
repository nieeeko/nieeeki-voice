"""Tests fuer cloud.py: GitHub, Vercel und Supabase direkt, mit Risikoklassen und Freigaben.

Ausfuehren:  uv run python -m unittest -v test_cloud.py   (im Ordner voice-core/)
Kein Test spricht echt mit GitHub, Vercel oder Supabase: gh und HTTP sind ersetzt.
"""

import json
import unittest
from unittest import mock

import cloud


class FakeGh:
    """Antwortet je nach den ersten Argumenten; merkt sich alle Aufrufe."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[list[str]] = []

    async def __call__(self, args, timeout=30):
        self.calls.append(args)
        for prefix, answer in self.answers.items():
            if args[: len(prefix)] == list(prefix):
                return answer
        return 0, "", ""


class FakeHttp:
    def __init__(self, answers: dict):
        self.answers = answers  # (method, teil der adresse) -> (status, data)
        self.calls: list[tuple] = []

    async def __call__(self, method, url, headers, body=None, timeout=20):
        self.calls.append((method, url, headers, body))
        for (m, part), answer in self.answers.items():
            if method == m and part in url:
                return answer
        return 404, {"error": {"message": "nicht da"}}


ENV = {"VERCEL_API_TOKEN": "vercel-geheim-123", "SUPABASE_ACCESS_TOKEN": "sbp_geheim_456"}


def tools(gh=None, http=None, env=ENV):
    return cloud.CloudTools(gh=gh or FakeGh({}), http=http or FakeHttp({}), env=dict(env))


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(cloud.CloudTools, "_audit")  # kein Audit-File im Test
        self.audit = patcher.start()
        self.addCleanup(patcher.stop)


class GithubTests(Base):
    def gh(self, extra=None):
        answers = {("api", "user"): (0, "nieeeko\n", "")}
        answers.update(extra or {})
        return FakeGh(answers)

    async def test_lesen_sofort_mit_eigenem_konto(self):
        gh = self.gh({("pr", "list"): (0, json.dumps([{"number": 3, "title": "HUD", "headRefName": "feat/hud",
                                                           "isDraft": False}]), "")})
        result = await tools(gh).call("github", {"aktion": "prs", "repo": "nieeeki-voice"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["prs"][0]["nummer"], 3)
        self.assertIn(["pr", "list", "-R", "nieeeko/nieeeki-voice"], [c[:4] for c in gh.calls])

    async def test_mergen_nur_nach_freigabe(self):
        pr = {"number": 10, "title": "HUD", "state": "OPEN", "headRefName": "feat/hud", "baseRefName": "main"}
        gh = self.gh({("pr", "view"): (0, json.dumps(pr), ""), ("pr", "merge"): (0, "", "")})
        t = tools(gh)
        result = await t.call("github", {"aktion": "pr_mergen", "repo": "nieeeko/nieeeki-voice", "nummer": 10})
        self.assertEqual(result["status"], "needs_approval")
        self.assertEqual((result["job"], result["dienst"]), ("cloud", "GitHub"))
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))  # noch nichts gemergt
        done = await t.decide(result["approval_id"], True)
        self.assertEqual(done["status"], "success")
        self.assertIn(["pr", "merge", "10", "-R", "nieeeko/nieeeki-voice", "--merge"], gh.calls)
        again = await t.decide(result["approval_id"], True)  # nur einmal
        self.assertEqual(again["status"], "error")

    async def test_abgelehnt_fuehrt_nichts_aus(self):
        gh = self.gh({("repo", "edit"): (0, "", "")})
        t = tools(gh)
        result = await t.call("github", {"aktion": "sichtbarkeit", "repo": "Test", "wert": "privat"})
        self.assertEqual((await t.decide(result["approval_id"], False))["status"], "rejected")
        self.assertFalse(any(c[:2] == ["repo", "edit"] for c in gh.calls))

    async def test_loeschen_gibt_es_nicht(self):
        for aktion in ["loeschen", "delete", "repo_loeschen", ""]:
            with self.subTest(aktion=aktion):
                result = await tools(self.gh()).call("github", {"aktion": aktion, "repo": "Test"})
                self.assertEqual(result["status"], "rejected")

    async def test_ungueltige_namen(self):
        t = tools(self.gh())
        for repo in ["../x", "a/b/c", "x;rm -rf", "", "nieeeko/name.git.git"]:
            with self.subTest(repo=repo):
                self.assertEqual((await t.call("github", {"aktion": "status", "repo": repo}))["status"], "error")
        for branch in ["", "-x", "a..b", "feat//x", "x.", "mit leerzeichen"]:
            with self.subTest(branch=branch):
                r = await t.call("github", {"aktion": "branch_anlegen", "repo": "Test", "branch": branch})
                self.assertEqual(r["status"], "error")

    async def test_branch_von_haupt_branch(self):
        gh = self.gh({
            ("repo", "view"): (0, json.dumps({"defaultBranchRef": {"name": "main"}}), ""),
            ("api", "repos/nieeeko/radar/git/ref/heads/main"): (0, "abc123\n", ""),
        })
        result = await tools(gh).call("github", {"aktion": "branch_anlegen", "repo": "radar", "branch": "feat/neu"})
        self.assertEqual(result["status"], "success")
        self.assertIn(["api", "-X", "POST", "repos/nieeeko/radar/git/refs", "-f", "ref=refs/heads/feat/neu",
                       "-f", "sha=abc123"], gh.calls)

    async def test_schreiben_nur_in_eigenen_repos(self):
        gh = self.gh({("issue", "create"): (0, "url", ""), ("pr", "list"): (0, "[]", "")})
        t = tools(gh)
        fremd = await t.call("github", {"aktion": "issue_anlegen", "repo": "fremder/projekt", "titel": "Spam"})
        self.assertEqual(fremd["status"], "error")
        self.assertFalse(any(c[:2] == ["issue", "create"] for c in gh.calls))
        lesen = await t.call("github", {"aktion": "prs", "repo": "fremder/projekt"})  # lesen geht ueberall
        self.assertEqual(lesen["status"], "success")

    async def test_titel_als_ein_argument(self):
        gh = self.gh({("issue", "create"): (0, "https://github.com/nieeeko/radar/issues/1\n", "")})
        await tools(gh).call("github", {"aktion": "issue_anlegen", "repo": "radar", "titel": "a; rm -rf / && x"})
        call = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        self.assertIn("a; rm -rf / && x", call)  # unveraendert als ein argv-Eintrag, keine Shell


class VercelTests(Base):
    async def test_deployments_eines_projekts(self):
        http = FakeHttp({
            ("GET", "/v9/projects/nieeeki-voice"): (200, {"id": "prj_1"}),
            ("GET", "/v6/deployments"): (200, {"deployments": [{"uid": "dpl_a", "url": "a.vercel.app", "state": "ERROR",
                                                               "target": None, "meta": {"githubCommitRef": "feat/x"}}]}),
        })
        result = await tools(http=http).call("vercel", {"aktion": "deployments", "projekt": "nieeeki-voice"})
        self.assertEqual(result["deployments"][0]["zustand"], "error")
        self.assertEqual(http.calls[0][2]["Authorization"], "Bearer vercel-geheim-123")

    async def test_preview_sofort_produktion_mit_freigabe(self):
        preview = {"id": "dpl_p", "name": "nieeeki-voice", "url": "p.vercel.app", "target": None, "readyState": "READY"}
        prod = {**preview, "id": "dpl_live", "target": "production", "projectId": "prj_1"}
        http = FakeHttp({("GET", "/v13/deployments/dpl_p"): (200, preview),
                         ("GET", "/v13/deployments/dpl_live"): (200, prod),
                         ("POST", "/v13/deployments"): (200, {"id": "dpl_neu", "url": "neu.vercel.app"}),
                         ("POST", "/promote/"): (200, {})})
        t = tools(http=http)
        self.assertEqual((await t.call("vercel", {"aktion": "neu_bauen", "deployment": "dpl_p"}))["status"], "success")
        result = await t.call("vercel", {"aktion": "neu_bauen", "deployment": "dpl_live"})
        self.assertEqual(result["status"], "needs_approval")
        live = await t.call("vercel", {"aktion": "produktion", "deployment": "dpl_live"})
        self.assertEqual(live["status"], "needs_approval")
        self.assertFalse(any("promote" in c[1] for c in http.calls))
        self.assertEqual((await t.decide(live["approval_id"], True))["status"], "success")
        self.assertTrue(any("/v10/projects/prj_1/promote/dpl_live" in c[1] for c in http.calls))

    async def test_ohne_token_klare_meldung(self):
        result = await tools(env={}).call("vercel", {"aktion": "projekte"})
        self.assertEqual(result["status"], "error")
        self.assertIn("VERCEL_API_TOKEN", result["summary"])

    async def test_token_landet_nie_im_ergebnis(self):
        http = FakeHttp({("GET", "/v9/projects"): (200, {"projects": [{"name": "leak vercel-geheim-123"}]})})
        result = await tools(http=http).call("vercel", {"aktion": "projekte"})
        self.assertNotIn("vercel-geheim-123", json.dumps(result))


class SupabaseTests(Base):
    PROJECTS = [{"id": "devref", "name": "shared-devtest"}, {"id": "prodref", "name": "nieeeki-prod"}]

    def http(self, rows=None):
        return FakeHttp({("GET", "/v1/projects"): (200, self.PROJECTS),
                         ("POST", "/database/query"): (200, rows if rows is not None else [{"n": 1}])})

    async def test_lesen_sofort_und_nur_lesend(self):
        http = self.http()
        result = await tools(http=http).call("supabase", {"aktion": "sql", "projekt": "shared-devtest",
                                                          "sql": "select count(*) as n from memories"})
        self.assertEqual(result["status"], "success")
        self.assertTrue(http.calls[-1][3]["read_only"])

    async def test_schreiben_mit_freigabe_auf_dev(self):
        http = self.http()
        t = tools(http=http)
        result = await t.call("supabase", {"aktion": "sql", "projekt": "shared-devtest",
                                           "sql": "update memories set is_active = false where id = 1"})
        self.assertEqual(result["status"], "needs_approval")
        self.assertFalse(any("database/query" in c[1] for c in http.calls))
        await t.decide(result["approval_id"], True)
        self.assertFalse(http.calls[-1][3]["read_only"])

    async def test_prod_schreiben_gesperrt(self):
        result = await tools(http=self.http()).call("supabase", {"aktion": "sql", "projekt": "nieeeki-prod",
                                                                 "sql": "drop table memories"})
        self.assertEqual(result["status"], "error")
        self.assertIn("PROD", result["summary"])

    def test_lesend_erkennen(self):
        for sql in ["select 1", "WITH x AS (select 1) select * from x", "explain select 1", "select 1;"]:
            with self.subTest(sql=sql):
                self.assertTrue(cloud.is_read_sql(sql))
        for sql in ["select 1; drop table x", "delete from x", "select * into neu from x", "create table x(a int)",
                    "with d as (delete from x returning *) select * from d", ""]:
            with self.subTest(sql=sql):
                self.assertFalse(cloud.is_read_sql(sql))

    async def test_ohne_token_erklaert_den_weg(self):
        result = await tools(env={}).call("supabase", {"aktion": "projekte"})
        self.assertIn("SUPABASE_ACCESS_TOKEN", result["summary"])

    async def test_schema_wird_geprueft(self):
        result = await tools(http=self.http()).call("supabase", {"aktion": "tabellen", "projekt": "shared-devtest",
                                                                 "schema": "public'; drop table x; --"})
        self.assertEqual(result["status"], "error")


class FreigabeTests(Base):
    async def test_unbekannte_und_kaputte_ids(self):
        t = tools()
        for aid in ["", "zzzzzzzz", None, 12345678, "../../x"]:
            with self.subTest(aid=aid):
                self.assertEqual((await t.decide(aid, True))["status"], "error")

    async def test_grosse_ergebnisse_werden_gekuerzt(self):
        http = FakeHttp({("GET", "/v1/projects"): (200, [{"id": "r", "name": "dev"}]),
                         ("POST", "/database/query"): (200, [{"text": "x" * 120}] * 200)})
        result = await tools(http=http).call("supabase", {"aktion": "sql", "projekt": "dev", "sql": "select 1"})
        self.assertLessEqual(len(json.dumps(result)), cloud.MAX_RESULT_CHARS + 500)


if __name__ == "__main__":
    unittest.main()
