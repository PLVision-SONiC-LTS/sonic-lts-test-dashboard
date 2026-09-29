import json
import requests

class AllureSuitesParser:
    """
    A parser to extract and summarize test results from an Allure suites JSON report.

    Attributes:
        content (dict): Parsed JSON content from Allure's suites.json (or API response).
        suite_results (list): A list of dictionaries summarizing test results per suite.
        general_stats (dict): Overall summary of test results across all suites.
        env_json (dict): Parsed environment.json data (optional, contains DUT info)
        summary_json (dict): Parsed summary.json data (optional, contains statistics like
                             total tests, broken/skipped/unknown, duration).
    """

    def __init__(self, data, env_json=None, summary_json=None, unit_env_key="HwSKU"):
        """
        Initialize the AllureSuitesParser.

        Args:
            data (dict): JSON data from suites.json.
            env_json (dict, optional): JSON data from environment.json. Defaults to {}.
            summary_json (dict, optional): JSON data from summary.json. Defaults to {}.
            unit_env_key (str): environment.json entry naming the unit under test.
        """
        self.content = data
        self.env_json = env_json or {}
        self.summary_json = summary_json or {}
        self.unit_env_key = unit_env_key
        self.suite_results = []
        self.general_stats = {}

    def parse_report(self):
        """
        Parse suites.json content and build a summary per suite.

        Each suite result includes:
            - name (str): Suite name
            - passed, failed, broken, skipped, unknown (int): Test counts
            - total (int): Sum of all test outcomes
            - status (str): "Pass" if no failures/broken tests, otherwise "Fail"
        """
        for suite in self.content.get('children', []):
            suite_name = suite.get('name', 'Unnamed Suite')
            stats = self.count_test_statuses(suite)
            stats["name"] = suite_name
            stats["total"] = sum(stats[status] for status in ["passed", "failed", "broken", "skipped", "unknown"])
            stats["status"] = "Pass" if stats["failed"] == 0 and stats["broken"] == 0 else "Fail"
            self.suite_results.append(stats)
        self.prepare_general_summary_report()

    def prepare_general_summary_report(self):
        """Populate self.general_stats with overall aggregated statistics."""
        self.get_statistics()

    def get_dut_name(self):
        """Extract the unit-under-test name from environment.json (configured key)."""

        if not self.env_json:
            return "Unknown"
        return next((item["values"][0] for item in self.env_json if item["name"] == self.unit_env_key), "Unknown")

    def get_statistics(self):
        """Compute overall aggregated statistics across all suites.

        NOTE: It was agreed within the team that tests marked as
        broken or unknown are to be considered as failed.
        That’s why the broken and unknown statuses are commented out."""
        total_stats = {"passed": 0, "failed": 0, "broken": 0, "skipped": 0, "unknown": 0, "total": 0}
        for suite in self.suite_results:
            for key in total_stats:
                if key in suite:
                    total_stats[key] += suite[key]

        total_stats["passed_percentage"] = round((total_stats["passed"] / total_stats["total"]) * 100, 2) if total_stats["total"] else 0
        total_stats["failed_percentage"] = round((total_stats["failed"] / total_stats["total"]) * 100, 2) if total_stats["total"] else 0
        total_stats["failed_percentage"] = round(((total_stats["failed"] + total_stats["broken"] + total_stats["unknown"]) / total_stats["total"]) * 100, 2) if total_stats["total"] else 0
        total_stats["skipped_percentage"] = round((total_stats["skipped"] / total_stats["total"]) * 100, 2) if total_stats["total"] else 0
        # total_stats["broken_percentage"] = round((total_stats["broken"]  / total_stats["total"]) * 100, 2) if total_stats["total"] else 0
        # total_stats["unknown_percentage"] = round((total_stats["unknown"] / total_stats["total"]) * 100, 2) if total_stats["total"] else 0
        total_stats["DUT"] = self.get_dut_name()

        self.general_stats = total_stats

    def count_test_statuses(self, node):
        """
        Recursively count test statuses for a suite or test node.

        NOTE: It was agreed within the team that tests marked as
        broken or unknown are to be considered as failed.
        That’s why the broken and unknown statuses are cleared.

        Args:
            node (dict): A node from suites.json containing test information.

        Returns:
            dict: Dictionary with counts for passed, failed, broken, skipped, unknown.
        """
        stats = {"passed": 0, "failed": 0, "broken": 0, "skipped": 0, "unknown": 0}
        if isinstance(node, dict):
            status = node.get("status", "").lower()
            if status in stats:
                stats[status] += 1
            for child in node.get("children", []):
                child_stats = self.count_test_statuses(child)
                for key in stats:
                    stats[key] += child_stats[key]

        # combine broken and unknown into failed
        stats["failed"] += stats["broken"] + stats["unknown"]
        stats["broken"] = 0
        stats["unknown"] = 0

        return stats

    def print_results(self):
        """Print summarized results for each suite in a human-readable format."""
        for suite in self.suite_results:
            print(
                f"Test Suite: {suite['name']}, Passed: {suite['passed']}, "
                f"Failed: {suite['failed']}, Broken: {suite['broken']}, "
                f"Skipped: {suite['skipped']}, Unknown: {suite['unknown']}, "
                f"Total: {suite['total']}, Status: {suite['status']}"
            )


if __name__ == "__main__":
    import os

    BASE_URL = os.getenv(
        "ALLURE_REPORT_URL",
        "http://localhost:5050/allure-docker-service/projects/my-project/reports/latest",
    )

    suites_json = requests.get(f"{BASE_URL}/data/suites.json").json()
    env_json = requests.get(f"{BASE_URL}/widgets/environment.json").json()
    summary_json = requests.get(f"{BASE_URL}/widgets/summary.json").json()

    parser = AllureSuitesParser(data=suites_json, env_json=env_json, summary_json=summary_json)
    parser.parse_report()
    parser.print_results()
    print(parser.suite_results)
    print(parser.general_stats)
