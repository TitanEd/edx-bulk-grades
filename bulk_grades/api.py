"""
Bulk Grading API.
"""

import logging
from collections import OrderedDict, defaultdict
from itertools import product

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Exists, OuterRef
from django.utils.functional import cached_property
from django.utils.translation import gettext as _
from lms.djangoapps.grades import api as grades_api
from opaque_keys.edx.keys import CourseKey, UsageKey
from openedx.core.djangoapps.course_groups.cohorts import get_cohort
from super_csv.csv_processor import CSVProcessor, DeferrableMixin, ValidationError


from .clients import LearnerAPIClient
from .models import ScoreOverrider

__all__ = ('GradeCSVProcessor', 'ScoreCSVProcessor', 'get_score', 'get_scores', 'set_score')

log = logging.getLogger(__name__)

UNKNOWN_LAST_SCORE_OVERRIDER = 'unknown'

def _get_enrollments(course_id, track=None, cohort=None, active_only=False, excluded_course_roles=None):
    """
    Return iterator of enrollment dictionaries.
    """
    enrollments = apps.get_model('student', 'CourseEnrollment').objects.filter(course_id=course_id).select_related(
        'user').prefetch_related('programcourseenrollment_set')
    if track:
        enrollments = enrollments.filter(mode=track)
    if cohort:
        # Handle cohort as ID (e.g., '1') instead of name (e.g., 'Cohort1')
        try:
            cohort_id = int(cohort)
            enrollments = enrollments.filter(
                user__cohortmembership__course_id=course_id,
                user__cohortmembership__course_user_group_id=cohort_id
            )
        except ValueError:
            # Fallback to name-based filtering if cohort is not an ID
            enrollments = enrollments.filter(
                user__cohortmembership__course_id=course_id,
                user__cohortmembership__course_user_group__name=cohort
            )
    if active_only:
        enrollments = enrollments.filter(is_active=True)
    if excluded_course_roles:
        course_access_role_filters = {
            "user": OuterRef('user'),
            "course_id": course_id
        }
        if excluded_course_roles != ['all']:
            course_access_role_filters['role__in'] = excluded_course_roles
        enrollments = enrollments.annotate(has_excluded_role=Exists(
            apps.get_model('student', 'CourseAccessRole').objects.filter(**course_access_role_filters)
        ))
        enrollments = enrollments.exclude(has_excluded_role=True)

    enrollment_count = enrollments.count()
    log.info(f"Found {enrollment_count} enrollments for course {course_id}, cohort {cohort}, roles {excluded_course_roles}")
    if enrollment_count == 0:
        log.warning(f"No enrollments found for course {course_id}, cohort {cohort}, roles {excluded_course_roles}")
    for enrollment in enrollments:
        enrollment_dict = {
            'user': enrollment.user,
            'user_id': enrollment.user.id,
            'username': enrollment.user.username,
            'full_name': enrollment.user.profile.name or '',
            'enrolled': enrollment.is_active,
            'track': enrollment.mode,
        }
        program_course_enrollment = enrollment.programcourseenrollment_set.all()
        if program_course_enrollment.exists():
            enrollment_dict['student_uid'] = program_course_enrollment.first().program_enrollment.external_user_key
        else:
            enrollment_dict['student_uid'] = None
        yield enrollment_dict

class ScoreCSVProcessor(DeferrableMixin, CSVProcessor):
    """
    CSV Processor for file format defined for Staff Graded Points.
    """
    columns = ['user_id', 'username', 'full_name', 'student_uid',
               'enrolled', 'track', 'cohort', 'block_id', 'title', 'date_last_graded',
               'who_last_graded', 'Previous Points', 'New Points']
    required_columns = ['user_id', 'New Points', 'block_id', 'Previous Points']

    size_to_defer = 100
    max_file_size = 4 * 1024 * 1024
    handle_undo = False

    def __init__(self, **kwargs):
        """
        Create a ScoreCSVProcessor.
        """
        self.max_points = 1
        self.user_id = None
        self.track = None
        self.cohort = None
        self.display_name = ''
        super().__init__(**kwargs)
        self._users_seen = set()

    def get_unique_path(self):
        """
        Return a unique id for CSVOperations.
        """
        return self.block_id

    def validate_row(self, row):
        """
        Validate CSV row.
        """
        super().validate_row(row)
        if row['block_id'] != self.block_id:
            raise ValidationError(_('The CSV does not match this problem. Check that you uploaded the right CSV.'))
        if row['New Points']:
            try:
                points = float(row['New Points'])
            except ValueError as error:
                raise ValidationError(_('Points must be numbers.')) from error
            if points > self.max_points:
                raise ValidationError(_('Points must not be greater than {}.').format(self.max_points))
            if points < 0:
                raise ValidationError(_('Points must be greater than 0'))

    def preprocess_row(self, row):
        """
        Preprocess CSV row.
        """
        if row['New Points'] and row['user_id'] not in self._users_seen:
            to_save = {
                'user_id': row['user_id'],
                'block_id': self.block_id,
                'new_points': float(row['New Points']),
                'max_points': self.max_points,
                'override_user_id': self.user_id,
            }
            self._users_seen.add(row['user_id'])
            return to_save

    def process_row(self, row):
        """
        Set the score for the given row, returning (status, undo).
        """
        if self.handle_undo:
            undo = get_score(row['block_id'], row['user_id'])
            undo['new_points'] = undo['score']
            undo['max_points'] = row['max_points']
        else:
            undo = None
        set_score(row['block_id'], row['user_id'], row['new_points'], row['max_points'], row['override_user_id'])
        return True, undo

    def get_rows_to_export(self):
        """
        Return iterator of rows for file export.
        """
        location = UsageKey.from_string(self.block_id)
        my_name = self.display_name
        students = get_scores(location)
        course_key = location.course_key
        enrollments = _get_enrollments(course_key, track=self.track, cohort=self.cohort)
        for enrollment in enrollments:
            cohort = get_cohort(enrollment['user'], course_key, assign=False)
            row = {
                'block_id': location,
                'title': my_name,
                'New Points': None,
                'Previous Points': None,
                'date_last_graded': None,
                'who_last_graded': None,
                'user_id': enrollment['user_id'],
                'username': enrollment['username'],
                'full_name': enrollment['full_name'],
                'student_uid': enrollment['student_uid'],
                'enrolled': enrollment['enrolled'],
                'track': enrollment['track'],
                'cohort': cohort.name if cohort else None,
            }
            score = students.get(enrollment['user_id'], None)
            if score:
                row['Previous Points'] = float(score['score'])
                row['date_last_graded'] = score['modified'].strftime('%Y-%m-%d %H:%M')
                row['who_last_graded'] = score['who_last_graded']
            yield row

    def commit(self, running_task=None):
        """
        Commit the data and trigger course grade recalculation.
        """
        super().commit(running_task=running_task)
        if running_task or not self.status()['waiting']:
            course_key = UsageKey.from_string(self.block_id).course_key
            grades_api.task_compute_all_grades_for_course.apply_async(kwargs={'course_key': str(course_key)})

class GradedSubsectionMixin:
    """
    Mixin to help generate lists of graded subsections and appropriate column names.
    """
    def append_columns(self, new_column_names):
        """
        Appends items from new_column_names to self.columns if not already present.
        """
        current_columns = set(self.columns)
        for new_column_name in new_column_names:
            if new_column_name not in current_columns:
                self.columns.append(new_column_name)

    @staticmethod
    def _get_graded_subsections(course_id, filter_subsection=None, filter_assignment_type=None):
        """
        Return list of graded subsections.
        """
        subsections = OrderedDict()
        for subsection in grades_api.graded_subsections_for_course_id(course_id):
            block_id = str(subsection.location.block_id)
            if (filter_subsection and (block_id != filter_subsection.block_id)) or \
               (filter_assignment_type and (filter_assignment_type != str(subsection.format))):
                continue
            short_block_id = block_id[:8]
            if short_block_id not in subsections:
                subsections[short_block_id] = (subsection, subsection.display_name)
        return subsections

    @staticmethod
    def _subsection_column_names(short_subsection_ids, prefixes):
        """
        Return list of column names from product of subsection IDs and prefixes.
        """
        return [f'{prefix}-{short_id}' for short_id, prefix in product(short_subsection_ids, prefixes)]

def decode_utf8(input_iterator):
    """
    Generator that decodes a utf-8 encoded input line by line.
    """
    for line in input_iterator:
        yield line if isinstance(line, str) else line.decode('utf-8')

class GradeCSVProcessor(DeferrableMixin, GradedSubsectionMixin, CSVProcessor):
    """
    CSV Processor for subsection grades.
    """
    required_columns = ['user_id', 'course_id']
    subsection_prefixes = ('name', 'grade', 'original_grade', 'previous_override', 'new_override')

    def __init__(self, **kwargs):
        """
        Create GradeCSVProcessor.
        """
        self.course_id = None
        self.subsection_grade_max = None
        self.subsection_grade_min = None
        self.course_grade_min = None
        self.course_grade_max = None
        self.subsection = None
        self.track = None
        self.cohort = None
        self.user_id = None
        self.active_only = False
        self.excluded_course_roles = None

        super().__init__(**kwargs)
        self.columns = ['User ID', 'Username', 'Email', 'Full Name', 'Percent(%)', 'Course ID', 'Track', 'Cohort']
        try:
            self._course_key = CourseKey.from_string(self.course_id) if self.course_id else None
        except Exception as e:
            log.error(f"Failed to parse course_id {self.course_id}: {str(e)}")
            raise
        self._subsection = UsageKey.from_string(self.subsection) if self.subsection else None
        self._subsections = self._get_graded_subsections(
            self._course_key,
            filter_subsection=self._subsection,
            filter_assignment_type=kwargs.get('assignment_type', None),
        )
        if self._subsection:
            self.append_columns(
                self._subsection_column_names(
                    self._subsections.keys(),
                    self.subsection_prefixes
                )
            )
        self._users_seen = defaultdict(list)
        self._row_num = 0
        log.info(f"Initialized GradeCSVProcessor: course={self.course_id}, cohort={self.cohort}, "
                 f"columns={self.columns}")

    @cached_property
    def _user(self):
        if self.user_id:
            return get_user_model().objects.get(id=self.user_id)

    def save(self, operation_name=None, operating_user=None):
        """
        Saves the operation state for this processor.
        """
        return super().save(operating_user=self._user)

    def get_unique_path(self):
        """
        Return a unique id for CSVOperations.
        """
        return self.course_id

    def validate_row(self, row):
        """
        Validate row.
        """
        super().validate_row(row)
        if row['course_id'] != self.course_id:
            raise ValidationError(_('Wrong course id {} != {}').format(row['course_id'], self.course_id))

    def preprocess_file(self, reader):
        """
        Preprocess the file, saving original data.
        """
        self._row_num = 0
        super().preprocess_file(reader)
        self.save()

    def preprocess_row(self, row):
        """
        Preprocess the CSV row.
        """
        self._row_num += 1
        operation = {}
        user_id = row['user_id']
        if user_id in self._users_seen:
            if len(self._users_seen[user_id]) == 1:
                self.add_error(_('Repeated user_id: ') + str(user_id), self._users_seen[user_id][0])
            self._users_seen[user_id].append(self._row_num)
            raise ValidationError(_('Repeated user_id: ') + str(user_id))
        self._users_seen[user_id].append(self._row_num)

        operation['new_override_grades'] = []
        operation['course_id'] = self.course_id
        operation['user_id'] = user_id

        for key in row:
            if key.startswith('new_override-'):
                value = row[key].strip()
                if value:
                    short_id = key.split('-', 1)[1]
                    subsection = self._subsections[short_id][0]
                    block_id = str(subsection.location)
                    try:
                        new_grade = float(value)
                    except ValueError as error:
                        raise ValidationError(_('Grade must be a number')) from error
                    if new_grade < 0:
                        raise ValidationError(_('Grade must not be negative'))
                    operation['new_override_grades'].append((block_id, new_grade))

        return operation

    def process_row(self, row):
        """
        Save a row to the persistent subsection override table.
        """
        for block_id, new_grade in row['new_override_grades']:
            grades_api.override_subsection_grade(
                row['user_id'],
                row['course_id'],
                block_id,
                overrider=self._user,
                earned_graded=new_grade,
                feature='grade-import',
                comment='Bulk Grade Import',
            )
        return True, None

    def get_rows_to_export(self):
        """
        Return iterator of rows to export.
        """
        try:
            enrollments = list(_get_enrollments(
                self._course_key,
                track=self.track,
                cohort=self.cohort,
                active_only=self.active_only,
                excluded_course_roles=self.excluded_course_roles,
            ))
            log.info(f"Exporting {len(enrollments)} users for course {self._course_key}, cohort {self.cohort}, "
                     f"columns={self.columns}")
            if not enrollments:
                log.warning(f"No enrollments found for course {self._course_key}, cohort {self.cohort}")
            enrolled_users = [enroll['user'] for enroll in enrollments]

            grades_api.prefetch_course_and_subsection_grades(self._course_key, enrolled_users)
            for enrollment in enrollments:
                cohort = get_cohort(enrollment['user'], self._course_key, assign=False)
                row = {
                    'User ID': enrollment['user_id'],
                    'Username': enrollment['username'],
                    'Email': enrollment['user'].email,
                    'Full Name': enrollment['full_name'],
                    'Percent(%)': '0 %',
                    'Course ID': self.course_id,
                    'Track': enrollment['track'],
                    'Cohort': cohort.name if cohort else '',
                }
                course_grade = grades_api.CourseGradeFactory().read(enrollment['user'], course_key=self._course_key)
                if course_grade:
                    row['Percent(%)'] = f"{course_grade.percent * 100:.1f} %"
                    log.info(f"Course grade for user {enrollment['user_id']}: {row['Percent(%)']}")
                else:
                    log.warning(f"No course grade for user {enrollment['user_id']} in course {self._course_key}")

                if self._subsection:
                    grades = grades_api.get_subsection_grades(enrollment['user_id'], self._course_key)
                    short_id = self._subsection.block_id[:8]
                    filtered_subsection, display_name = self._subsections[short_id]
                    subsection_grade = grades.get(filtered_subsection.location, None)
                    if subsection_grade:
                        try:
                            effective_grade = (subsection_grade.override.earned_graded_override /
                                               subsection_grade.override.possible_graded_override) * 100
                        except AttributeError:
                            effective_grade = (subsection_grade.earned_graded /
                                               subsection_grade.possible_graded) * 100
                        row[f'grade-{short_id}'] = effective_grade
                        row[f'name-{short_id}'] = display_name
                        if (self.subsection_grade_min and effective_grade < self.subsection_grade_min) or \
                           (self.subsection_grade_max and effective_grade > self.subsection_grade_max):
                            continue

                if self.course_grade_min or self.course_grade_max:
                    course_grade_normalized = course_grade.percent * 100 if course_grade else 0
                    if (self.course_grade_min and course_grade_normalized < self.course_grade_min) or \
                       (self.course_grade_max and course_grade_normalized > self.course_grade_max):
                        continue

                yield row
        except Exception as e:
            log.error(f"Error in get_rows_to_export for course {self._course_key}, cohort {self.cohort}: {str(e)}")
            raise

    def filtered_column_headers(self):
        """
        Return filtered list of columns to export.
        """
        columns = self.columns.copy()
        if self._subsection:
            columns.extend(self._subsection_column_names(self._subsections.keys(), self.subsection_prefixes))
        log.info(f"Filtered column headers for course {self.course_id}: {columns}")
        return columns

class AbsoluteGradeCSVProcessor(GradeCSVProcessor):
    """
    CSV Processor for subsection grades with absolute scores.
    """
    def __init__(self, **kwargs):
        """
        Create AbsoluteGradeCSVProcessor.
        """
        super().__init__(**kwargs)
        # Override columns to remove 'Percent(%)', 'Track', 'Cohort' and add subsection display names
        self.columns = ['User ID', 'Username', 'Email', 'Full Name', 'Course ID']
        self.append_columns(
            [display_name for _, display_name in self._subsections.values()]
        )
        log.info(f"Initialized AbsoluteGradeCSVProcessor: course={self.course_id}, cohort={self.cohort}, "
                 f"columns={self.columns}")

    def get_rows_to_export(self):
        """
        Return iterator of rows to export with absolute scores.
        """
        try:
            enrollments = list(_get_enrollments(
                self._course_key,
                track=self.track,
                cohort=self.cohort,
                active_only=self.active_only,
                excluded_course_roles=['staff', 'instructor']
            ))
            log.info(f"Exporting {len(enrollments)} users for course {self._course_key}, cohort={self.cohort}, "
                     f"columns={self.columns}")
            if not enrollments:
                log.warning(f"No enrollments found for course {self._course_key}, cohort={self.cohort}")
            enrolled_users = [enroll['user'] for enroll in enrollments]

            grades_api.prefetch_course_and_subsection_grades(self._course_key, enrolled_users)
            for enrollment in enrollments:
                row = {
                    'User ID': enrollment['user_id'],
                    'Username': enrollment['username'],
                    'Email': enrollment['user'].email,
                    'Full Name': enrollment['full_name'],
                    'Course ID': self.course_id,
                }

                grades = grades_api.get_subsection_grades(enrollment['user_id'], self._course_key)
                for short_id, (subsection, display_name) in self._subsections.items():
                    subsection_grade = grades.get(subsection.location, None)
                    if subsection_grade:
                        if getattr(subsection_grade, 'override', None):
                            score_earned = subsection_grade.override.earned_graded_override
                            score_possible = subsection_grade.override.possible_graded_override
                        else:
                            score_earned = subsection_grade.earned_graded
                            score_possible = subsection_grade.possible_graded
                        row[display_name] = f"{int(score_earned)}/{int(score_possible)}" if score_possible > 0 else "0/0"
                    else:
                        row[display_name] = "0/0"

                    if subsection_grade and (self.subsection_grade_min or self.subsection_grade_max):
                        effective_grade = ((subsection_grade.override.earned_graded_override /
                                            subsection_grade.override.possible_graded_override) * 100
                                           if getattr(subsection_grade, 'override', None)
                                           else (subsection_grade.earned_graded / subsection_grade.possible_graded) * 100
                                          if subsection_grade.possible_graded > 0 else 0)
                        if (self.subsection_grade_min and effective_grade < self.subsection_grade_min) or \
                           (self.subsection_grade_max and effective_grade > self.subsection_grade_max):
                            continue

                course_grade = grades_api.CourseGradeFactory().read(enrollment['user'], course_key=self._course_key)
                if self.course_grade_min or self.course_grade_max:
                    course_grade_normalized = course_grade.percent * 100 if course_grade else 0
                    if (self.course_grade_min and course_grade_normalized < self.course_grade_min) or \
                       (self.course_grade_max and course_grade_normalized > self.course_grade_max):
                        continue

                yield row
        except Exception as e:
            log.error(f"Error in get_rows_to_export for course {self._course_key}, cohort={self.cohort}: {str(e)}")
            raise

    def filtered_column_headers(self):
        """
        Return filtered list of columns to export.
        """
        columns = self.columns.copy()
        log.info(f"Filtered column headers for course {self.course_id}: {columns}")
        return columns

class InterventionCSVProcessor(GradedSubsectionMixin, CSVProcessor):
    """
    CSV Processor for intervention report grades for masters track only.
    """
    MASTERS_TRACK = 'masters'
    subsection_prefixes = ('name', 'grade')

    def __init__(self, **kwargs):
        """
        Create InterventionCSVProcessor.
        """
        self.columns = [
            'user_id', 'username', 'email', 'student_key', 'full_name', 'course_id', 'track', 'cohort',
            'number of videos overall', 'number of videos last week', 'number of problems overall',
            'number of problems last week', 'number of correct problems overall',
            'number of correct problems last week', 'number of problem attempts overall',
            'number of problem attempts last week', 'number of forum posts overall',
            'number of forum posts last week', 'date last active',
        ]
        self.course_id = None
        self.cohort = None
        self.subsection = None
        self.assignment_type = None
        self.subsection_grade_min = None
        self.subsection_grade_max = None
        self.course_grade_min = None
        self.course_grade_max = None

        super().__init__(**kwargs)
        try:
            self._course_key = CourseKey.from_string(self.course_id) if self.course_id else None
        except Exception as e:
            log.error(f"Failed to parse course_id {self.course_id}: {str(e)}")
            raise
        self._subsection = UsageKey.from_string(self.subsection) if self.subsection else None
        self._subsections = self._get_graded_subsections(
            self._course_key,
            filter_subsection=self._subsection,
            filter_assignment_type=self.assignment_type,
        )
        self.append_columns(
            self._subsection_column_names(self._subsections.keys(), self.subsection_prefixes)
        )
        self.append_columns(('course grade letter', 'course grade numeric'))
        log.info(f"Initialized InterventionCSVProcessor: course={self.course_id}, cohort={self.cohort}")

    def get_rows_to_export(self):
        """
        Return iterator of rows to export.
        """
        try:
            enrollments = list(_get_enrollments(self._course_key, track=self.MASTERS_TRACK, cohort=self.cohort))
            log.info(f"Exporting {len(enrollments)} users for InterventionCSVProcessor, course={self._course_key}, "
                     f"cohort={self.cohort}")
            if not enrollments:
                log.warning(f"No enrollments found for InterventionCSVProcessor, course={self._course_key}, "
                            f"cohort={self.cohort}")
            grades_api.prefetch_course_and_subsection_grades(self._course_key, [enroll['user'] for enroll in enrollments])
            client = LearnerAPIClient()
            intervention_list = client.courses(self.course_id).user_engagement().get()
            intervention_data = {val['username']: val for val in intervention_list}
            for enrollment in enrollments:
                grades = grades_api.get_subsection_grades(enrollment['user_id'], self._course_key)
                if self._subsection and (self.subsection_grade_max or self.subsection_grade_min):
                    short_id = self._subsection.block_id[:8]
                    filtered_subsection, _ = self._subsections[short_id]
                    subsection_grade = grades.get(filtered_subsection.location, None)
                    if not subsection_grade:
                        continue
                    try:
                        effective_grade = (subsection_grade.override.earned_graded_override /
                                           subsection_grade.override.possible_graded_override) * 100
                    except AttributeError:
                        effective_grade = (subsection_grade.earned_graded /
                                           subsection_grade.possible_graded) * 100
                    if (self.subsection_grade_min and effective_grade < self.subsection_grade_min) or \
                       (self.subsection_grade_max and effective_grade > self.subsection_grade_max):
                        continue
                course_grade = grades_api.CourseGradeFactory().read(enrollment['user'], course_key=self._course_key)
                if self.course_grade_min or self.course_grade_max:
                    course_grade_normalized = course_grade.percent * 100 if course_grade else 0
                    if (self.course_grade_min and course_grade_normalized < self.course_grade_min) or \
                       (self.course_grade_max and course_grade_normalized > self.course_grade_max):
                        continue

                cohort = get_cohort(enrollment['user'], self._course_key, assign=False)
                int_user = intervention_data.get(enrollment['username'], {})
                row = {
                    'user_id': enrollment['user_id'],
                    'username': enrollment['username'],
                    'email': enrollment['user'].email,
                    'student_key': enrollment['student_uid'],
                    'full_name': enrollment['full_name'],
                    'track': enrollment['track'],
                    'course_id': self.course_id,
                    'cohort': cohort.name if cohort else None,
                    'number of videos overall': int_user.get('videos_overall', 0),
                    'number of videos last week': int_user.get('videos_last_week', 0),
                    'number of problems overall': int_user.get('problems_overall', 0),
                    'number of problems last week': int_user.get('problems_last_week', 0),
                    'number of correct problems overall': int_user.get('correct_problems_overall', 0),
                    'number of correct problems last week': int_user.get('correct_problems_last_week', 0),
                    'number of problem attempts overall': int_user.get('problems_attempts_overall', 0),
                    'number of problem attempts last week': int_user.get('problems_attempts_last_week', 0),
                    'number of forum posts overall': int_user.get('forum_posts_overall', 0),
                    'number of forum posts last week': int_user.get('forum_posts_last_week', 0),
                    'date last active': int_user.get('date_last_active', ''),
                    'course grade letter': course_grade.letter_grade if course_grade else '',
                    'course grade numeric': course_grade.percent if course_grade else 0.0
                }
                for block_id, (subsection, display_name) in self._subsections.items():
                    row[f'name-{block_id}'] = display_name
                    grade = grades.get(subsection.location, None)
                    if grade:
                        row[f'grade-{block_id}'] = (grade.override.earned_graded_override
                                                    if getattr(grade, 'override', None)
                                                    else grade.earned_graded)
                yield row
        except Exception as e:
            log.error(f"Error in get_rows_to_export for InterventionCSVProcessor, course {self._course_key}: {str(e)}")
            raise

def set_score(usage_key, student_id, score, max_points, override_user_id=None, **defaults):
    """
    Set a score.
    """
    if not isinstance(usage_key, UsageKey):
        usage_key = UsageKey.from_string(usage_key)
    defaults['module_type'] = 'problem'
    if score < 0:
        raise ValueError(_('score must be positive'))
    defaults['grade'] = score
    defaults['max_grade'] = max_points
    module = apps.get_model('courseware', 'StudentModule').objects.update_or_create(
        student_id=student_id,
        course_id=usage_key.course_key,
        module_state_key=usage_key,
        defaults=defaults)[0]
    if override_user_id:
        ScoreOverrider.objects.create(
            module=module,
            user_id=override_user_id)

def get_score(usage_key, user_id):
    """
    Return score for user_id and usage_key.
    """
    try:
        return get_scores(usage_key, [user_id])[int(user_id)]
    except KeyError:
        return None

def get_scores(usage_key, user_ids=None):
    """
    Return dictionary of student_id: scores.
    """
    if not isinstance(usage_key, UsageKey):
        usage_key = UsageKey.from_string(usage_key)
    scores_qset = apps.get_model('courseware', 'StudentModule').objects.filter(
        course_id=usage_key.course_key,
        module_state_key=usage_key,
    )
    if user_ids:
        scores_qset = scores_qset.filter(student_id__in=user_ids)

    scores = {}
    for row in scores_qset:
        scores[row.student_id] = {
            'score': row.grade,
            'max_grade': row.max_grade,
            'created': row.created,
            'modified': row.modified,
            'state': row.state,
        }
        try:
            last_override = row.scoreoverrider_set.select_related('user').latest('created')
        except ObjectDoesNotExist:
            scores[row.student_id]['who_last_graded'] = UNKNOWN_LAST_SCORE_OVERRIDER
        else:
            scores[row.student_id]['who_last_graded'] = last_override.user.username
    return scores


try:
    from custom_extensions.waffle import ENABLE_ABSOLUTE_GRADES_CSV  # Import custom waffle switch
    GradeCSVProcessor = AbsoluteGradeCSVProcessor if ENABLE_ABSOLUTE_GRADES_CSV.is_enabled() else GradeCSVProcessor
except Exception as e:
    log.error(f"Error in ENABLE_ABSOLUTE_GRADES_CSV.is_enabled(): {str(e)}")
    GradeCSVProcessor = GradeCSVProcessor