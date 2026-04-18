import unittest
import sqlite3
import os
import json
from datetime import datetime
from unittest.mock import MagicMock
import sys

# Local mock for structlog before importing Database
mock_structlog = MagicMock()
sys.modules["structlog"] = mock_structlog

from src.core.db import Database

class TestDatabaseSecurity(unittest.TestCase):
    def setUp(self):
        self.db_path = "test_security.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.db = Database(self.db_path)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_update_job_security_and_timestamp(self):
        # Insert a job
        job_id = "test_job_1"
        self.db.insert_job({
            "id": job_id,
            "fingerprint": "fp1",
            "url": "http://example.com",
            "status": "new"
        })

        # Initial state
        initial_job = self.db.get_jobs(status="new")[0]
        initial_updated_at = initial_job.get("updated_at")

        # Test update and ensure updated_at changes
        import time
        time.sleep(0.1) # Ensure timestamp would actually change if it was using seconds, but it's ISO format

        self.db.update_job(job_id, status="parsed")

        updated_job = self.db.get_jobs(status="parsed")[0]
        self.assertEqual(updated_job["status"], "parsed")
        self.assertNotEqual(updated_job["updated_at"], initial_updated_at)
        self.assertIsNotNone(updated_job["updated_at"])

    def test_update_run_normal(self):
        # Insert a run
        run_id = "run_normal_test"
        self.db.insert_run({
            "id": run_id,
            "started_at": datetime.utcnow().isoformat()
        })

        self.db.update_run(run_id, jobs_found=5)

        # Verify update
        row = self.db.conn.execute("SELECT jobs_found FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["jobs_found"], 5)

if __name__ == "__main__":
    unittest.main()
