import os, time

from django.db import models
from django.db.models import Max, Min, Count, F
from django.core.urlresolvers import reverse
from django.core.files.storage import FileSystemStorage

from archive_chan.settings import AppSettings

# This overrides the global media url.
fs = FileSystemStorage(base_url=AppSettings.get('MEDIA_URL'))

class Board(models.Model):
    name = models.CharField(max_length=255, primary_key = True)
    active = models.BooleanField(
        default=True,
        help_text='Should this board be updated with new posts?'
    )
    store_threads_for = models.IntegerField(
        default=48,
        help_text='[hours] After that much time passes from the last reply in a NOT SAVED thread it will be deleted. Set to 0 to preserve threads forever.'
    )
    replies_threshold = models.IntegerField(
        default=20,
        help_text='Store threads after they reach that many replies.'
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return format("/%s/" % self.name)

    def get_absolute_url(self):
        return reverse('archive_chan:board', args=[self.name])


class Thread(models.Model):
    board = models.ForeignKey('Board')
    number = models.IntegerField()
    saved = models.BooleanField(default=False) # Threads which are not saved will get deleted after some time.
    auto_saved = models.BooleanField(default=False) # Was this thread saved automatically by a trigger?
    tags = models.ManyToManyField('Tag', through='TagToThread')

    replies = models.IntegerField(default=0)
    images = models.IntegerField(default=0)
    first_reply = models.DateTimeField(null=True, default=None)
    last_reply = models.DateTimeField(null=True, default=None)

    # Used by scraper.
    def last_reply_time(self):
        last = self.post_set.last()
        if last is None:
            return None
        else:
            return last.time

    # Used by scraper.
    def count_replies(self):
        return self.post_set.count() - 1

    # Used by board template. Can't figure out a query which wouldd fetch everything in one go.
    def first_post(self):
        return self.post_set.select_related('image').first()

    class Meta:
        unique_together = ('board', 'number')

    def __str__(self):
        return format("#%s" % (self.number))

    def get_absolute_url(self):
        return reverse('archive_chan:thread', args=[self.board.name, self.number])


class Post(models.Model):
    thread = models.ForeignKey('Thread')

    number = models.IntegerField()
    time = models.DateTimeField()

    name = models.CharField(max_length=255, blank=True)
    trip = models.CharField(max_length=255, blank=True)
    email = models.CharField(max_length=255, blank=True)
    country = models.CharField(max_length=2, blank=True)

    subject = models.TextField(blank=True)
    comment = models.TextField(blank=True)

    save_time = models.DateTimeField(auto_now_add = True)

    def is_main(self):
        return (self.number == self.thread.number)

    class Meta:
        ordering = ['number']

    def __str__(self):
        return format("#%s" % (self.number))

    def get_absolute_url(self):
        return '%s#post-%s' % (self.thread.get_absolute_url(), self.number)

class Image(models.Model):
    original_name = models.CharField(max_length=255)
    post = models.OneToOneField('Post')
    image = models.FileField(upload_to = "post_images", storage=fs) # It is impossible to use ImageField to store webm.
    thumbnail = models.FileField(upload_to = "post_thumbnails", storage=fs)

    def get_extension(self):
        name, extension = os.path.splitext(self.image.name)
        return extension


class Trigger(models.Model):
    FIELD_CHOICES = (
        ('name', 'Name'),
        ('trip', 'Trip'),
        ('email', 'Email'),
        ('subject', 'Subject'),
        ('comment', 'Comment'),
    )

    EVENT_CHOICES = (
        ('contains', 'Contains'),
        ('containsno', 'Doesn\'t contain'),
        ('is', 'Is'),
        ('isnot', 'Is not'),
        ('begins', 'Begins with'),
        ('ends', 'Ends with'),
    )

    POST_TYPE_CHOICES=(
        ('any', 'Any post'),
        ('master', 'First post'),
        ('sub', 'Reply'),
    )
    
    field = models.CharField(max_length=10, choices=FIELD_CHOICES)
    event = models.CharField(max_length=10, choices=EVENT_CHOICES)
    phrase = models.CharField(max_length=255, blank=True)
    case_sensitive = models.BooleanField(default=True)
    post_type = models.CharField(max_length=10, choices=POST_TYPE_CHOICES)

    save_thread = models.BooleanField(default=False, help_text='Save the thread.')
    tag_thread = models.ForeignKey('Tag', blank=True, null=True, default=None, help_text='Add this tag to the thread.')

    active = models.BooleanField(default=True)


class TagToThread(models.Model):
    thread = models.ForeignKey('Thread')
    tag = models.ForeignKey('Tag')
    automatically_added = models.BooleanField(default=False, editable=False)
    save_time = models.DateTimeField(auto_now_add = True)

    def __str__(self):
        return format('%s - %s' % (self.thread, self.tag))


class Tag(models.Model):
    name = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return self.name


class Update(models.Model):
    CURRENT = 0
    FAILED = 1
    COMPLETED = 2

    STATUS_CHOICES = (
        (CURRENT, 'Started'),
        (FAILED, 'Failed'),
        (COMPLETED, 'Completed'),
    )

    board = models.ForeignKey('Board')
    status = models.SmallIntegerField(choices=STATUS_CHOICES, default=CURRENT)
    start = models.DateTimeField()
    end = models.DateTimeField(null=True)

    used_threads = models.IntegerField()

    total_time = models.FloatField(default=0)
    wait_time = models.FloatField(default=0)
    download_time = models.FloatField(default=0)

    processed_threads = models.IntegerField(default=0)
    added_posts = models.IntegerField(default=0)
    removed_posts = models.IntegerField(default=0)

    downloaded_images = models.IntegerField(default=0)
    downloaded_thumbnails = models.IntegerField(default=0)
    downloaded_threads = models.IntegerField(default=0)

    class Meta:
        ordering = ['-start']

from django.db.models.signals import pre_delete, post_save, pre_delete, post_delete
from django.dispatch.dispatcher import receiver

@receiver(pre_delete, sender=Image)
def pre_image_delete(sender, instance, **kwargs):
    """Delete images from the HDD."""
    instance.image.delete(False)
    instance.thumbnail.delete(False)

@receiver(post_save, sender=Image)
def post_image_save(sender, instance, created, **kwargs):
    """Update images."""
    if created:
        thread = instance.post.thread
        thread.images += 1
        thread.save()

@receiver(post_save, sender=Post)
def post_post_save(sender, instance, created, **kwargs):
    """Update the replies, last_reply and first_reply."""
    if created:
        thread = instance.thread

        # Replies.
        thread.replies += 1

        # Last reply.
        if thread.last_reply is None or instance.time > thread.last_reply:
            thread.last_reply = instance.time

        # First reply.
        if thread.first_reply is None or instance.time < thread.first_reply:
            thread.first_reply = instance.time

        thread.save()

@receiver(post_delete, sender=Post)
def post_post_delete(sender, instance, **kwargs):
    """Update replies, images, last_reply, first_reply."""
    thread = instance.thread

    # Replies
    thread.replies -= 1

    # Images.
    try:
        if instance.image:
            thread.images -= 1

    except:
        pass

    # First and last reply.
    if thread.first_reply == instance.time or thread.last_reply == instance.time:
        thread_recount = Thread.objects.annotate(
            min_post=Min('post__time'),
            max_post=Max('post__time')
        ).get(pk=instance.thread.pk)
        
        thread.first_reply=thread_recount.min_post
        thread.last_reply=thread_recount.max_post

    thread.save()
