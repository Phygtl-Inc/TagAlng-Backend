"""Meets are embedded as RETRIEVAL_DOCUMENT and asks against them as RETRIEVAL_QUERY.

Vertex lines a short search up with a paragraph better when told which is which, and the
gain needs BOTH sides in their modes — so the writer, the browse query and the backfill
must agree.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.event_embed import EVENT_DOC_TASK, EVENT_QUERY_TASK


def _client(dim=768):
    c = MagicMock()
    c.models.embed_content.return_value = SimpleNamespace(
        embeddings=[SimpleNamespace(values=[0.1] * dim)]
    )
    return c


class VertexEmbedModeTests(unittest.TestCase):
    def test_a_task_type_reaches_vertex(self):
        from app import vertex_extract

        c = _client()
        with patch.object(vertex_extract, "_vertex_client", return_value=c):
            vertex_extract.vertex_embed("jazz", task_type="RETRIEVAL_QUERY")
        cfg = c.models.embed_content.call_args.kwargs["config"]
        self.assertEqual(cfg.task_type, "RETRIEVAL_QUERY")

    def test_no_task_type_keeps_the_default_mode_for_every_other_caller(self):
        from app import vertex_extract

        c = _client()
        with patch.object(vertex_extract, "_vertex_client", return_value=c):
            vertex_extract.vertex_embed("a claim")
        self.assertNotIn("config", c.models.embed_content.call_args.kwargs)


class EventHelpersTests(unittest.TestCase):
    def test_meets_are_documents_and_asks_are_queries(self):
        from app import event_embed

        with patch("app.vertex_extract.vertex_embed", return_value=[0.1]) as ve:
            event_embed.embed_event_document("Latin Jazz Concert — live band")
            event_embed.embed_event_query("jazz")
        self.assertEqual(ve.call_args_list[0].kwargs["task_type"], EVENT_DOC_TASK)
        self.assertEqual(ve.call_args_list[1].kwargs["task_type"], EVENT_QUERY_TASK)
        self.assertEqual((EVENT_DOC_TASK, EVENT_QUERY_TASK), ("RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"))

    def test_failures_are_none_never_raise(self):
        from app import event_embed

        with patch("app.vertex_extract.vertex_embed", side_effect=RuntimeError("down")):
            self.assertIsNone(event_embed.embed_event_document("x"))
            self.assertIsNone(event_embed.embed_event_query("x"))
        self.assertIsNone(event_embed.embed_event_query("   "))


class PublishWriterTests(unittest.TestCase):
    def test_a_published_meet_is_stored_as_a_document_vector(self):
        from app import event_publish

        table = MagicMock()
        sb = MagicMock()
        sb.table.return_value = table
        table.update.return_value = table
        table.eq.return_value = table
        started = []
        with patch.object(event_publish, "service_client", return_value=sb), patch(
            "app.event_embed.embed_event_document", return_value=[0.2] * 768
        ) as doc, patch("threading.Thread") as th:
            th.side_effect = lambda target, **kw: SimpleNamespace(start=lambda: started.append(target))
            event_publish._embed_event_async("ev1", {"title": "Latin Jazz Concert"})
            started[0]()
        self.assertIn("Latin Jazz Concert", doc.call_args.args[0])
        self.assertEqual(table.update.call_args.args[0]["embedding"], [0.2] * 768)


class BackfillModeTests(unittest.TestCase):
    def test_the_backfill_and_its_probe_use_the_same_modes_as_the_worker(self):
        import scripts.backfill_event_embeddings as bf

        c = _client()
        with patch.dict("os.environ", {"GCP_VERTEX_PROJECT": "p"}), patch(
            "google.genai.Client", return_value=c
        ):
            bf._vertex_embed("meet text", task_type=EVENT_DOC_TASK)
        self.assertEqual(c.models.embed_content.call_args.kwargs["config"].task_type,
                         "RETRIEVAL_DOCUMENT")
        src = open(bf.__file__).read()
        self.assertIn("_vertex_embed(text, task_type=EVENT_DOC_TASK)", src)
        self.assertIn("_vertex_embed(ask, task_type=EVENT_QUERY_TASK)", src)


if __name__ == "__main__":
    unittest.main()
