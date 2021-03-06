from django.shortcuts import get_object_or_404
from django.db.models import Max, Min, Count, Q, F
from django.views.generic import ListView, TemplateView

from archive_chan.models import Board, Thread, Post
import archive_chan.lib.modifiers as modifiers

class BodyIdMixin(object):
    """This mixin adds an easy way to add body_id to the context."""
    def get_context_data(self, **kwargs):
        context = super(BodyIdMixin, self).get_context_data(**kwargs)
        context['body_id'] = getattr(self, 'body_id', None)
        return context


class UniversalViewMixin(BodyIdMixin):
    """This mixin automatically adds board_name and thread_number to the context."""
    def get_context_data(self, **kwargs):
        context = super(UniversalViewMixin, self).get_context_data(**kwargs)
        context['board_name'] = self.kwargs.get('board', None)
        context['thread_number'] = int(self.kwargs['thread']) if 'thread' in self.kwargs else None
        return context


class IndexView(BodyIdMixin, ListView):
    """View showing all boards."""
    model = Board
    context_object_name = 'board_list'
    template_name = 'archive_chan/index.html'
    body_id = 'body-home'


class BoardView(BodyIdMixin, ListView):
    """View showing all threads in a specified board."""
    model = Thread
    context_object_name = 'thread_list'
    template_name = 'archive_chan/board.html'
    paginate_by = 20
    body_id = 'body-board'

    available_parameters = {
        'sort': (
            ('last_reply', ('Last reply', 'last_reply', None)),
            ('creation_date', ('Creation date', 'first_reply', None)),
            ('replies', ('Replies', 'replies', None)),
            ('images', ('Images', 'images', None)),
        ),
        'saved': (
            ('all', ('All', None)),
            ('yes', ('Yes', {'saved': True})),
            ('no',  ('No', {'saved': False})),
        ),
        'last_reply': (
            ('always', ('Always', None)),
            ('quarter', ('15 minutes', {'last_reply__gt': 0.25})),
            ('hour', ('Hour', {'last_reply__gt': 1})),
            ('day', ('Day', {'last_reply__gt': 24})),
            ('week', ('Week', {'last_reply__gt': 24 * 7})),
            ('month', ('Month', {'last_reply__gt': 24 * 30})),
        ),
        'tagged': (
            ('all', ('All', None)),
            ('yes', ('Yes', {'tags__isnull': False})),
            ('auto', ('Automatically', {'tagtothread__automatically_added': True})),
            ('user', ('Manually', {'tagtothread__automatically_added': False})),
            ('no', ('No', {'tags__isnull': True})),
        )
    }

    def get_parameters(self):
        """Extracts parameters related to filtering and sorting from a request object."""
        parameters = {}

        self.modifiers = {}

        self.modifiers['sort'] = modifiers.SimpleSort(
            self.available_parameters['sort'],
            self.request.GET.get('sort', None)
        )

        self.modifiers['saved'] = modifiers.SimpleFilter(
            self.available_parameters['saved'],
            self.request.GET.get('saved', None)
        )

        self.modifiers['tagged'] = modifiers.SimpleFilter(
            self.available_parameters['tagged'],
            self.request.GET.get('tagged', None)
        )

        self.modifiers['last_reply'] = modifiers.TimeFilter(
            self.available_parameters['last_reply'],
            self.request.GET.get('last_reply', None)
        )

        self.modifiers['tag'] = modifiers.TagFilter(
            self.request.GET.get('tag', None)
        )

        parameters['sort'], parameters['sort_reverse'] = self.modifiers['sort'].get()
        parameters['sort_with_operator'] = self.modifiers['sort'].get_full()
        parameters['saved'] = self.modifiers['saved'].get()
        parameters['tagged'] = self.modifiers['tagged'].get()
        parameters['last_reply'] = self.modifiers['last_reply'].get()
        parameters['tag'] = self.modifiers['tag'].get()

        return parameters

    def get_queryset(self):
        self.parameters = self.get_parameters()

        # I don't know how to select all data I need using the ORM without executing
        # TWO damn additional queries for each thread (first post + tags).
        queryset = Thread.objects.filter(board=self.kwargs['board'], replies__gte=1).select_related('board')

        for key, modifier in self.modifiers.items():
            queryset = modifier.execute(queryset)

        return queryset

    def get_context_data(self, **kwargs):
        context = super(BoardView, self).get_context_data(**kwargs)
        context['board_name'] = self.kwargs['board']
        context['parameters'] = self.parameters
        context['available_parameters'] = self.available_parameters
        return context

class ThreadView(BodyIdMixin, ListView):
    """View showing all posts in a specified thread."""
    model = Post
    context_object_name = 'post_list'
    template_name = 'archive_chan/thread.html'
    body_id = 'body-thread'

    def get_queryset(self):
        board_name = self.kwargs['board']
        thread_number = self.kwargs['thread']
        return Post.objects.select_related('image', 'thread', 'thread__board').filter(
                thread__number=thread_number,
                thread__board=board_name
            )

    def get_context_data(self, **kwargs):
        thread = get_object_or_404(Thread, board=self.kwargs['board'], number=self.kwargs['thread'])

        context = super(ThreadView, self).get_context_data(**kwargs)
        context['board_name'] = self.kwargs['board']
        context['thread_number'] = int(self.kwargs['thread'])
        context['thread'] = thread 
        context['tags'] = thread.tagtothread_set.select_related('tag').order_by('tag__name')
        return context


class SearchView(UniversalViewMixin, ListView):
    """View showing all threads in a specified board."""
    model = Post
    context_object_name = 'post_list'
    template_name = 'archive_chan/search.html'
    paginate_by = 20
    body_id = 'body-search'

    available_parameters = {
        'type': (
            ('all', ('All', None)),
            ('op', ('Main post', {'number': F('thread__number')})),
        ),
        'saved': (
            ('all', ('All', None)),
            ('yes', ('Yes', {'thread__saved': True})),
            ('no',  ('No', {'thread__saved': False})),
        ),
        'created': (
            ('always', ('Always', None)),
            ('quarter', ('15 minutes', {'time': 0.25})),
            ('hour', ('Hour', {'time': 1})),
            ('day', ('Day', {'time': 24})),
            ('week', ('Week', {'time': 24 * 7})),
            ('month', ('Month', {'time': 24 * 30})),
        )
    }

    def get_parameters(self):
        """Extracts parameters related to filtering and sorting from a request object."""
        parameters = {}

        self.modifiers = {}

        self.modifiers['saved'] = modifiers.SimpleFilter(
            self.available_parameters['saved'],
            self.request.GET.get('saved', None)
        )

        self.modifiers['type'] = modifiers.SimpleFilter(
            self.available_parameters['type'],
            self.request.GET.get('type', None)
        )

        self.modifiers['created'] = modifiers.TimeFilter(
            self.available_parameters['created'],
            self.request.GET.get('created', None)
        )

        parameters['saved'] = self.modifiers['saved'].get()
        parameters['created'] = self.modifiers['created'].get()
        parameters['type'] = self.modifiers['type'].get()
        parameters['search'] = self.request.GET.get('search', None)

        return parameters

    def get_queryset(self):
        self.parameters = self.get_parameters()
        self.chart_data = None

        if self.parameters['search'] is None or len(self.parameters['search']) == 0:
            return Post.objects.none()

        queryset = Post.objects

        if 'board' in self.kwargs:
            queryset = queryset.filter(thread__board=self.kwargs['board'])

        if 'thread' in self.kwargs:
            queryset = queryset.filter(thread__number=self.kwargs['thread'])
        
        queryset = queryset.filter(
            Q(subject__icontains=self.parameters['search']) |
            Q(comment__icontains=self.parameters['search'])
        ).order_by('-time')
        
        for key, modifier in self.modifiers.items():
            queryset = modifier.execute(queryset)

        self.chart_data = queryset.extra({
            'date': 'date("time")',
        }).values('date').order_by('date').annotate(amount=Count('id'))

        return queryset.select_related('thread', 'image', 'thread__board')

    def get_context_data(self, **kwargs):
        from archive_chan.lib.stats import get_posts_chart_data

        context = super(SearchView, self).get_context_data(**kwargs)
        context['parameters'] = self.parameters
        context['available_parameters'] = self.available_parameters
        context['chart_data'] = get_posts_chart_data(self.chart_data)
        return context


class GalleryView(UniversalViewMixin, TemplateView):
    """View displaying gallery template. Data is loaded via AJAX calls."""
    template_name = 'archive_chan/gallery.html'
    body_id = 'body-gallery'


class StatsView(UniversalViewMixin, TemplateView):
    """View displaying stats template. Data is loaded via AJAX calls."""
    template_name = 'archive_chan/stats.html'
    body_id = 'body-stats'


class StatusView(BodyIdMixin, TemplateView):
    """View displaying archive status. Data is loaded via AJAX calls."""
    template_name = 'archive_chan/status.html'
    body_id = 'body-status'
