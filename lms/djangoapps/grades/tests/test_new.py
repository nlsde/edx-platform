"""
Test saved subsection grade functionality.
"""

import ddt
from django.conf import settings
from django.db.utils import DatabaseError
from mock import patch

from capa.tests.response_xml_factory import MultipleChoiceResponseXMLFactory
from courseware.tests.helpers import get_request_for_user
from lms.djangoapps.course_blocks.api import get_course_blocks
from lms.djangoapps.grades.config.tests.utils import persistent_grades_feature_flags
from student.models import CourseEnrollment
from student.tests.factories import UserFactory
from xmodule.modulestore.tests.django_utils import SharedModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory, ItemFactory

from ..models import PersistentSubsectionGrade
from ..new.course_grade import CourseGradeFactory
from ..new.subsection_grade import SubsectionGrade, SubsectionGradeFactory
from lms.djangoapps.grades.tests.utils import mock_get_score


class GradeTestBase(SharedModuleStoreTestCase):
    """
    Base class for Course- and SubsectionGradeFactory tests.
    """
    @classmethod
    def setUpClass(cls):
        super(GradeTestBase, cls).setUpClass()
        cls.course = CourseFactory.create()
        cls.chapter = ItemFactory.create(
            parent=cls.course,
            category="chapter",
            display_name="Test Chapter"
        )
        cls.sequence = ItemFactory.create(
            parent=cls.chapter,
            category='sequential',
            display_name="Test Sequential 1",
            graded=True
        )
        cls.vertical = ItemFactory.create(
            parent=cls.sequence,
            category='vertical',
            display_name='Test Vertical 1'
        )
        problem_xml = MultipleChoiceResponseXMLFactory().build_xml(
            question_text='The correct answer is Choice 3',
            choices=[False, False, True, False],
            choice_names=['choice_0', 'choice_1', 'choice_2', 'choice_3']
        )
        cls.problem = ItemFactory.create(
            parent=cls.vertical,
            category="problem",
            display_name="Test Problem",
            data=problem_xml
        )

    def setUp(self):
        super(GradeTestBase, self).setUp()
        self.request = get_request_for_user(UserFactory())
        self.client.login(username=self.request.user.username, password="test")
        self.course_structure = get_course_blocks(self.request.user, self.course.location)
        self.subsection_grade_factory = SubsectionGradeFactory(self.request.user, self.course, self.course_structure)
        CourseEnrollment.enroll(self.request.user, self.course.id)


@ddt.ddt
class TestCourseGradeFactory(GradeTestBase):
    """
    Test that CourseGrades are calculated properly
    """

    @patch.dict(settings.FEATURES, {'PERSISTENT_GRADES_ENABLED_FOR_ALL_TESTS': False})
    @ddt.data(
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    )
    @ddt.unpack
    def test_course_grade_feature_gating(self, feature_flag, course_setting):
        # Grades are only saved if the feature flag and the advanced setting are
        # both set to True.
        grade_factory = CourseGradeFactory(self.request.user)
        with persistent_grades_feature_flags(
            global_flag=feature_flag,
            enabled_for_all_courses=False,
            course_id=self.course.id,
            enabled_for_course=course_setting
        ):
            with patch('lms.djangoapps.grades.new.course_grade._pretend_to_save_course_grades') as mock_save_grades:
                grade_factory.create(self.course)
        self.assertEqual(mock_save_grades.called, feature_flag and course_setting)


@ddt.ddt
class SubsectionGradeFactoryTest(GradeTestBase):
    """
    Tests for SubsectionGradeFactory functionality.

    Ensures that SubsectionGrades are created and updated properly, that
    persistent grades are functioning as expected, and that the flag to
    enable saving subsection grades blocks/enables that feature as expected.
    """

    def test_create(self):
        """
        Tests to ensure that a persistent subsection grade is created, saved, then fetched on re-request.
        """
        with patch(
            'lms.djangoapps.grades.new.subsection_grade.PersistentSubsectionGrade.create_grade',
            wraps=PersistentSubsectionGrade.create_grade
        ) as mock_create_grade:
            with patch(
                'lms.djangoapps.grades.new.subsection_grade.SubsectionGradeFactory._get_saved_grade',
                wraps=self.subsection_grade_factory._get_saved_grade  # pylint: disable=protected-access
            ) as mock_get_saved_grade:
                with self.assertNumQueries(14):
                    grade_a = self.subsection_grade_factory.create(self.sequence)
                self.assertTrue(mock_get_saved_grade.called)
                self.assertTrue(mock_create_grade.called)

                mock_get_saved_grade.reset_mock()
                mock_create_grade.reset_mock()

                with self.assertNumQueries(0):
                    grade_b = self.subsection_grade_factory.create(self.sequence)
                self.assertTrue(mock_get_saved_grade.called)
                self.assertFalse(mock_create_grade.called)

        self.assertEqual(grade_a.url_name, grade_b.url_name)
        self.assertEqual(grade_a.all_total, grade_b.all_total)

    @ddt.data(
        (
            'lms.djangoapps.grades.new.subsection_grade.SubsectionGrade.create_model',
            lambda self: self.subsection_grade_factory.create(self.sequence)
        ),
        (
            'lms.djangoapps.grades.new.subsection_grade.SubsectionGrade.bulk_create_models',
            lambda self: self.subsection_grade_factory.bulk_create_unsaved()
        ),
    )
    @ddt.unpack
    def test_fallback_handling(self, underlying_method, method_to_test):
        """
        Tests that the persistent grades fallback handler functions as expected.
        """
        with patch('lms.djangoapps.grades.new.subsection_grade.log') as log_mock:
            with patch(underlying_method) as underlying:
                underlying.side_effect = DatabaseError("I'm afraid I can't do that")
                method_to_test(self)
                # By making it this far, we implicitly assert "the factory method swallowed the exception correctly"
                self.assertTrue(
                    log_mock.warning.call_args_list[0].startswith("Persistent Grades: Persistence Error, falling back.")
                )

    @patch.dict(settings.FEATURES, {'PERSISTENT_GRADES_ENABLED_FOR_ALL_TESTS': False})
    @ddt.data(
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    )
    @ddt.unpack
    def test_subsection_grade_feature_gating(self, feature_flag, course_setting):
        # Grades are only saved if the feature flag and the advanced setting are
        # both set to True.
        with patch(
            'lms.djangoapps.grades.models.PersistentSubsectionGrade.bulk_read_grades'
        ) as mock_read_saved_grade:
            with persistent_grades_feature_flags(
                global_flag=feature_flag,
                enabled_for_all_courses=False,
                course_id=self.course.id,
                enabled_for_course=course_setting
            ):
                self.subsection_grade_factory.create(self.sequence)
        self.assertEqual(mock_read_saved_grade.called, feature_flag and course_setting)


class SubsectionGradeTest(GradeTestBase):
    """
    Tests SubsectionGrade functionality.
    """

    def test_compute(self):
        """
        Assuming the underlying score reporting methods work, test that the score is calculated properly.
        """
        with mock_get_score(1, 2):
            grade = self.subsection_grade_factory.create(self.sequence)
        self.assertEqual(grade.all_total.earned, 1)
        self.assertEqual(grade.all_total.possible, 2)

    def test_save_and_load(self):
        """
        Test that grades are persisted to the database properly, and that loading saved grades returns the same data.
        """
        # Create a grade that *isn't* saved to the database
        input_grade = SubsectionGrade(self.sequence, self.course)
        input_grade.init_from_structure(
            self.request.user,
            self.course_structure,
            self.subsection_grade_factory._submissions_scores,  # pylint: disable=protected-access
            self.subsection_grade_factory._csm_scores,  # pylint: disable=protected-access
        )
        self.assertEqual(PersistentSubsectionGrade.objects.count(), 0)

        # save to db, and verify object is in database
        input_grade.create_model(self.request.user)
        self.assertEqual(PersistentSubsectionGrade.objects.count(), 1)

        # load from db, and ensure output matches input
        loaded_grade = SubsectionGrade(self.sequence, self.course)
        saved_model = PersistentSubsectionGrade.read_grade(
            user_id=self.request.user.id,
            usage_key=self.sequence.location,
        )
        loaded_grade.init_from_model(
            self.request.user,
            saved_model,
            self.course_structure,
            self.subsection_grade_factory._submissions_scores,  # pylint: disable=protected-access
            self.subsection_grade_factory._csm_scores,  # pylint: disable=protected-access
        )

        self.assertEqual(input_grade.url_name, loaded_grade.url_name)
        self.assertEqual(input_grade.all_total, loaded_grade.all_total)
