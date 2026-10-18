import unittest

from app import config
from app.linkedin_fresh_jobs import app


class SingleServerIntegrationTests(unittest.TestCase):
    def test_main_server_mounts_the_authorized_import_api(self):
        mount = next(route for route in app.routes if getattr(route, "name", None) == "job-import-api")
        paths = {route.path for route in mount.app.routes}
        self.assertIn("/jobs/import", paths)

    def test_combined_watcher_setting_is_boolean(self):
        self.assertIsInstance(config.settings.FRESH_WATCHER_IN_MAIN, bool)


if __name__ == "__main__":
    unittest.main()
