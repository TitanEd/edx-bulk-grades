"""
CSV import/export API for grades.
"""

import datetime

from django.http import HttpResponseForbidden, JsonResponse, StreamingHttpResponse
from django.views.generic import View

from . import api

class GradeOnlyExport(View):
    """
    CSV Export of grade information only. To be used by both bulk grade export and interventions.
    """
    def __init__(self, **kwargs):
        """
        Configure initial state.
        """
        super().__init__(**kwargs)
        self.processor = None
        self.extra_filename = ''

    def get_export_iterator(self, request):
        """
        Return an iterator appropriate for a streaming response.
        """
        return []

    def initialize_processor(self, request, course_id):
        """
        Abstract method to initialize processor particular to the class.
        """
        pass

    def dispatch(self, request, course_id, *args, **kwargs):  # pylint: disable=arguments-differ
        """
        Dispatch django request.
        """
        try:
            self.initialize_processor(request, course_id)
        except Exception as e:
            raise
        return super().dispatch(request, course_id, *args, **kwargs)

    def get_export_filename(self, course_id):
        """
        Create filename for export.
        """
        filename_elements = [course_id]
        if self.extra_filename:
            filename_elements.append(self.extra_filename)
        filename_elements.append(datetime.datetime.utcnow().isoformat())
        return '-'.join(filename_elements) + '.csv'

    def get(self, request, course_id, *args, **kwargs):
        """
        Export grades in CSV format.

        GET arguments:
        track: name of enrollment mode
        cohort: name of cohort
        subsection: block id of graded subsection
        """
        try:
            iterator = self.get_export_iterator(request)
            filename = self.get_export_filename(course_id)
            response = StreamingHttpResponse(iterator, content_type='text/csv')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
        except Exception as e:
            raise

class GradeImportExport(GradeOnlyExport):
    """
    CSV Grade import/export view.
    """
    def post(self, request, course_id, *args, **kwargs):
        """
        Import grades from a CSV file.
        """
        try:
            result_id = request.POST.get('result_id', None)
            if result_id:
                results = self.processor.get_deferred_result(result_id)
                if results.ready():
                    data = results.get()
                else:
                    data = {'waiting': True, 'result_id': result_id}
            else:
                the_file = request.FILES['csv']
                self.processor.process_file(the_file, autocommit=True)
                data = self.processor.status()
                data['error_messages'] = []
                for error_message in self.processor.error_messages:
                    line_numbers = [str(line_number+1) for line_number in self.processor.error_messages[error_message]]
                    is_plural = 's' if len(line_numbers) > 1 else ''
                    new_message = f'{error_message} (on line{is_plural} {", ".join(line_numbers)})'
                    data['error_messages'].append(new_message)
            return JsonResponse(data)
        except Exception as e:
            raise

    def get_export_iterator(self, request):
        """
        Create an iterator for exporting grade data.
        """
        error_id = request.GET.get('error_id', '')
        return self.processor.get_iterator(error_data=bool(error_id))

    def initialize_processor(self, request, course_id):
        """
        Initialize GradeCSVProcessor.
        """
        operation_id = request.GET.get('error_id', '')
        if operation_id:
            self.processor = api.GradeCSVProcessor.load(operation_id)
            self.processor.columns = self.processor.filtered_column_headers()
            if self.processor.course_id != course_id:
                return HttpResponseForbidden()
            self.extra_filename = 'graded-results'
        else:
            assignment_grade_max = request.GET.get('assignmentGradeMax')
            assignment_grade_min = request.GET.get('assignmentGradeMin')
            course_grade_min = request.GET.get('courseGradeMin')
            course_grade_max = request.GET.get('courseGradeMax')
            cohort = request.GET.get('cohort')
            excluded_course_roles = request.GET.getlist('excludedCourseRoles')
            self.processor = api.GradeCSVProcessor(
                course_id=course_id,
                user_id=request.user.id,
                track=request.GET.get('track'),
                cohort=cohort,
                subsection=request.GET.get('assignment'),
                assignment_type=request.GET.get('assignmentType'),
                subsection_grade_max=(float(assignment_grade_max) if assignment_grade_max else None),
                subsection_grade_min=(float(assignment_grade_min) if assignment_grade_min else None),
                course_grade_min=(float(course_grade_min) if course_grade_min else None),
                course_grade_max=(float(course_grade_max) if course_grade_max else None),
                excluded_course_roles=excluded_course_roles,
                active_only=True,
            )

class GradeOperationHistoryView(View):
    """
    Collection View for history of grade override file uploads.
    """
    def get(self, request, course_id):
        """
        Get all previous times grades have been overwritten for this course.
        """
        try:
            processor = api.GradeCSVProcessor(
                course_id=course_id,
                _user=request.user
            )
            history = processor.get_committed_history()
            return  JsonResponse(history, safe=False)
        except Exception as e:
            raise

class InterventionsExport(GradeOnlyExport):
    """
    Interventions export view.
    """
    extra_filename = 'intervention'

    def get_export_iterator(self, request):
        """
        Create an iterator for exporting intervention data.
        """
        return self.processor.get_iterator()

    def initialize_processor(self, request, course_id):
        """
        Initialize InterventionCSVProcessor.
        """
        assignment_grade_max = request.GET.get('assignmentGradeMax')
        assignment_grade_min = request.GET.get('assignmentGradeMin')
        course_grade_min = request.GET.get('courseGradeMin')
        course_grade_max = request.GET.get('courseGradeMax')
        cohort = request.GET.get('cohort')
        self.processor = api.InterventionCSVProcessor(
            course_id=course_id,
            _user=request.user,
            cohort=cohort,
            subsection=request.GET.get('assignment'),
            assignment_type=request.GET.get('assignmentType'),
            subsection_grade_min=(float(assignment_grade_min) if assignment_grade_min else None),
            subsection_grade_max=(float(assignment_grade_max) if assignment_grade_max else None),
            course_grade_min=(float(course_grade_min) if course_grade_min else None),
            course_grade_max=(float(course_grade_max) if course_grade_max else None)
        )