# coding=utf-8
"""Tests that sync and publish RPM repositories.

For information on repository sync and publish operations, see
`Synchronization`_ and `Publication`_.

.. _Publication:
    http://docs.pulpproject.org/en/latest/dev-guide/integration/rest-api/repo/publish.html
.. _Synchronization:
    http://docs.pulpproject.org/en/latest/dev-guide/integration/rest-api/repo/sync.html
"""
import inspect
import unittest
from urllib.parse import urljoin

from packaging.version import Version

from pulp_smash import api, config, exceptions, selectors, utils
from pulp_smash.constants import (
    DRPM_UNSIGNED_FEED_URL,
    ORPHANS_PATH,
    REPOSITORY_PATH,
    RPM_INCOMPLETE_FILELISTS_FEED_URL,
    RPM_INCOMPLETE_OTHER_FEED_URL,
    RPM_MISSING_FILELISTS_FEED_URL,
    RPM_MISSING_OTHER_FEED_URL,
    RPM_MISSING_PRIMARY_FEED_URL,
    RPM_SIGNED_FEED_COUNT,
    RPM_SIGNED_FEED_URL,
    SRPM_SIGNED_FEED_URL,
)
from pulp_smash.tests.rpm.api_v2.utils import gen_repo
from pulp_smash.tests.rpm.utils import set_up_module as setUpModule  # noqa pylint:disable=unused-import


_REPO_PUBLISH_PATH = '/pulp/repos/'  # + relative_url + unit_name.rpm.arch


# This class is left public for documentation purposes.
class SyncRepoBaseTestCase(utils.BaseAPITestCase):
    """A parent class for repository syncronization test cases.

    :meth:`get_feed_url` should be overridden by concrete child classes. This
    method's response is used when setting the repository's importer feed URL.
    """

    @classmethod
    def setUpClass(cls):
        """Create an RPM repository with a valid feed and sync it."""
        if inspect.getmro(cls)[0] == SyncRepoBaseTestCase:
            raise unittest.SkipTest('Abstract base class.')
        super(SyncRepoBaseTestCase, cls).setUpClass()
        client = api.Client(cls.cfg, api.json_handler)
        body = gen_repo()
        body['importer_config']['feed'] = cls.get_feed_url()
        cls.repo_href = client.post(REPOSITORY_PATH, body)['_href']
        cls.resources.add(cls.repo_href)
        cls.report = utils.sync_repo(cls.cfg, cls.repo_href)

    @staticmethod
    def get_feed_url():
        """Return an RPM repository feed URL. Should be overridden.

        :raises: ``NotImplementedError`` if not overridden by a child class.
        """
        raise NotImplementedError()

    def test_start_sync_code(self):
        """Assert the call to sync a repository returns an HTTP 202."""
        self.assertEqual(self.report.status_code, 202)

    def test_task_progress_report(self):
        """Assert no task's progress report contains error details.

        Other assertions about the final state of each task are handled by the
        client's response handler. (For more information, see the source of
        :func:`pulp_smash.api.safe_handler`.)
        """
        tasks = tuple(api.poll_spawned_tasks(self.cfg, self.report.json()))
        for i, task in enumerate(tasks):
            with self.subTest(i=i):
                error_details = task['progress_report']['yum_importer']['content']['error_details']  # noqa pylint:disable=line-too-long
                self.assertEqual(error_details, [], task)


class SyncRpmRepoTestCase(SyncRepoBaseTestCase):
    """Test one can create and sync an RPM repository with an RPM feed."""

    @staticmethod
    def get_feed_url():
        """Return an RPM repository feed URL."""
        return RPM_SIGNED_FEED_URL

    # This is specific to the RPM repo. Leave in this test case.
    def test_unit_count_on_repo(self):
        """Verify that the sync added the correct number of units to the repo.

        Read the repository and examine its ``content_unit_counts`` attribute.
        Compare these attributes to metadata from the remote repository.
        Expected values are currently hard-coded into this test.
        """
        content_unit_counts = {
            'rpm': RPM_SIGNED_FEED_COUNT,
            'erratum': 4,
            'package_group': 2,
            'package_category': 1,
        }
        if self.cfg.version >= Version('2.9'):  # langpack support added in 2.9
            content_unit_counts['package_langpacks'] = 1
        repo = api.Client(self.cfg).get(self.repo_href).json()
        self.assertEqual(repo['content_unit_counts'], content_unit_counts)

    def test_no_change_in_second_sync(self):
        """Verify that syncing a second time has no changes.

        If the repository have not changed then Pulp must state that anything
        was changed when doing a second sync.
        """
        report = utils.sync_repo(self.cfg, self.repo_href)
        tasks = tuple(api.poll_spawned_tasks(self.cfg, report.json()))
        with self.subTest(comment='spawned tasks'):
            self.assertEqual(len(tasks), 1)
        for count_type in ('added_count', 'removed_count', 'updated_count'):
            with self.subTest(comment=count_type):
                self.assertEqual(tasks[0]['result'][count_type], 0)


class SyncDrpmRepoTestCase(SyncRepoBaseTestCase):
    """Test one can create and sync an RPM repository with an DRPM feed."""

    @staticmethod
    def get_feed_url():
        """Return an DRPM repository feed URL."""
        return DRPM_UNSIGNED_FEED_URL


class SyncSrpmRepoTestCase(SyncRepoBaseTestCase):
    """Test one can create and sync an RPM repository with an SRPM feed."""

    @staticmethod
    def get_feed_url():
        """Return an SRPM repository feed URL."""
        return SRPM_SIGNED_FEED_URL


class SyncInvalidFeedTestCase(utils.BaseAPITestCase):
    """Create and sync an RPM repository with an invalid feed.

    The sync should complete with errors reported.
    """

    @classmethod
    def setUpClass(cls):
        """Create class-wide variables."""
        super(SyncInvalidFeedTestCase, cls).setUpClass()
        cls.tasks = []

    def test_01_set_up(self):
        """Create and sync an RPM repository with an invalid feed."""
        client = api.Client(self.cfg, api.json_handler)
        body = gen_repo()
        body['importer_config']['feed'] = utils.uuid4()
        repo = client.post(REPOSITORY_PATH, body)
        self.addCleanup(client.delete, repo['_href'])

        client.response_handler = api.echo_handler
        report = client.post(urljoin(repo['_href'], 'actions/sync/'))
        report.raise_for_status()
        self.tasks.extend(list(
            api.poll_spawned_tasks(self.cfg, report.json())
        ))
        with self.subTest(comment='verify call report status code'):
            self.assertEqual(report.status_code, 202)
        with self.subTest(comment='verify the number of spawned tasks'):
            self.assertEqual(len(self.tasks), 1, self.tasks)

    @selectors.skip_if(len, 'tasks', 0)
    def test_02_task_error_traceback(self):
        """Assert each task's "error" and "traceback" fields are non-null."""
        if selectors.bug_is_untestable(1455, self.cfg.version):
            self.skipTest('https://pulp.plan.io/issues/1455')
        for i, task in enumerate(self.tasks):
            for key in {'error', 'traceback'}:
                with self.subTest((i, key)):
                    self.assertIsNotNone(task[key], task)

    @selectors.skip_if(len, 'tasks', 0)
    def test_02_task_progress_report(self):
        """Assert each task's progress report contains error details."""
        if selectors.bug_is_untestable(1376, self.cfg.version):
            self.skipTest('https://pulp.plan.io/issues/1376')
        for i, task in enumerate(self.tasks):
            with self.subTest(i=i):
                self.assertNotEqual(
                    task['progress_report']['yum_importer']['content']['error_details'],  # noqa pylint:disable=line-too-long
                    [],
                    task,
                )


class SyncInvalidMetadataTestCase(unittest.TestCase):
    """Sync various repositories with invalid metadata.

    When a repository with invalid metadata is encountered, Pulp should
    gracefully fail. This test case targets `Pulp #1287
    <https://pulp.plan.io/issues/1287>`_.
    """

    @classmethod
    def tearDownClass(cls):
        """Delete orphan content units."""
        api.Client(config.get_config()).delete(ORPHANS_PATH)

    def test_incomplete_filelists(self):
        """Sync a repository with an incomplete ``filelists.xml`` file."""
        self.do_test(RPM_INCOMPLETE_FILELISTS_FEED_URL)

    def test_incomplete_other(self):
        """Sync a repository with an incomplete ``other.xml`` file."""
        self.do_test(RPM_INCOMPLETE_OTHER_FEED_URL)

    def test_missing_filelists(self):
        """Sync a repository that's missing its ``filelists.xml`` file."""
        self.do_test(RPM_MISSING_FILELISTS_FEED_URL)

    def test_missing_other(self):
        """Sync a repository that's missing its ``other.xml`` file."""
        self.do_test(RPM_MISSING_OTHER_FEED_URL)

    def test_missing_primary(self):
        """Sync a repository that's missing its ``primary.xml`` file."""
        self.do_test(RPM_MISSING_PRIMARY_FEED_URL)

    def do_test(self, feed_url):
        """Implement the logic described by each of the ``test*`` methods."""
        cfg = config.get_config()
        client = api.Client(cfg)
        body = gen_repo()
        body['importer_config']['feed'] = feed_url
        repo = client.post(REPOSITORY_PATH, body).json()
        self.addCleanup(client.delete, repo['_href'])

        with self.assertRaises(exceptions.TaskReportError) as context:
            utils.sync_repo(cfg, repo['_href'])
        task = context.exception.task
        self.assertEqual(
            'NOT_STARTED',
            task['progress_report']['yum_importer']['content']['state'],
            task,
        )
