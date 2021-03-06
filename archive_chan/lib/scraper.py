import requests, datetime, re, time, html, sys, threading

from django.utils.timezone import utc
from django.db import transaction
from django.core.files.base import ContentFile

from archive_chan.models import Thread, Post, Image, Trigger, TagToThread, Update
from archive_chan.settings import AppSettings

class ScrapError(Exception):
    pass

class ThreadInfo:
    """Class used for storing information about the thread."""

    def __init__(self, thread_json): 
        """This constructor loads the data from a part of the JSON retrieved from the catalog API."""

        # Get the thread number.
        self.number = thread_json['no']
        
        # Get the time of the last reply or thread creation time.
        if 'last_replies' in thread_json and len(thread_json) > 0:
            last_reply_time = int(thread_json['last_replies'][-1]['time'])
        else:
            last_reply_time = int(thread_json['time'])

        # Note the timezone-aware datetime.
        self.last_reply_time = datetime.datetime.fromtimestamp(last_reply_time).replace(tzinfo=utc)

        # Get the number of the replies in the thread (first post doest not count).
        self.replies = int(thread_json['replies'])


class PostData:
    """Class used for storing data about the post before saving it to the database."""

    def replace_content(self, text):
        """Cleans the HTML from the post comment. Returns a string."""

        # Remove >>quotes.
        text = re.sub(r'\<a.*?class="quotelink".*?\>(.*?)\</a\>', r"\1", text)

        # Remove spans: >le meme arrows/deadlinks etc.
        text = re.sub(r'\<span.*?\>(.*?)\</span\>', r"\1", text)

        # Code.
        text = re.sub(r'\<pre.*?\>(.*?)\</pre\>', r"[code]\1[/code]", text)

        # Newline.
        text = text.replace("<br>", "\n")

        # Word break opportunity.
        text = text.replace("<wbr>", "")

        # Unescape characters.
        text = html.unescape(text)

        return text


    def __init__(self, post_json): 
        """Constructor loads the data from JSON retrieved from the thread API."""

        self.number = int(post_json['no'])
        self.time = datetime.datetime.fromtimestamp(int(post_json['time'])).replace(tzinfo=utc)

        self.name = post_json.get('name', '')
        self.trip = post_json.get('trip', '')
        self.email = post_json.get('email', '')
        self.country = post_json.get('country', '')

        self.subject = self.replace_content(post_json.get('sub', ''))
        self.comment = self.replace_content(post_json.get('com', ''))

        self.filename = post_json.get('tim')
        self.extension = post_json.get('ext')
        self.original_filename = post_json.get('filename')


class Triggers:
    """Class handling triggers."""

    def __init__(self):
        # Prepare trigger list.
        self.triggers = Trigger.objects.filter(active=True)

    def check_post_type(self, trigger, thread, post_data):
        """True if the type of the post (master - first post, sub - reply) is correct,
        false otherwise.
        """
        # Sub post and we are looking for the master.
        if post_data.number != thread.number and trigger.post_type == 'master':
            return False

        # Master post and we are looking for the sub.
        if post_data.number == thread.number and trigger.post_type == 'sub':
            return False

        return True
    
    def check_event(self, trigger, post_data):
        """True if event terms are fulfilled, false otherwise."""
        field_value = str(getattr(post_data, trigger.field))
        trigger_value = trigger.phrase

        # Case insensitive?
        if not trigger.case_sensitive:
            field_value = field_value.lower()
            trigger_value = trigger_value.lower()

        # Check if the event occurs.
        if trigger.event == 'isnot' or trigger.event == 'containsno':
            return_value = False
        else:
            return_value = True

        if trigger.event == 'contains' or trigger.event == 'containsno':
            if field_value.find(trigger_value) >= 0:
                return return_value
            else:
                return not return_value

        if trigger.event == 'is' or trigger.event == 'isnot':
            if field_value == trigger_value:
                return return_value
            else:
                return not return_value

        if trigger.event == 'begins':
            return field_value.startswith(trigger_value)

        if trigger.event == 'ends':
            return field_value.endswith(trigger_value)

        return False


    def get_single_trigger_actions(self, trigger, thread, post_data):
        """Returns a set of actions to execute based on one trigger."""
        if not self.check_post_type(trigger, thread, post_data):
            return None
        
        actions = set()

        if self.check_event(trigger, post_data):
            if trigger.save_thread:
                actions.add(('save', 0))

            if trigger.tag_thread is not None:
                actions.add(('add_tag', trigger.tag_thread))

        return actions

    def get_actions(self, thread, post_data):
        """Returns a set of actions to execute."""
        actions = set()

        # Prepare a set of actions to execute.
        for trigger in self.triggers:
            new_actions = self.get_single_trigger_actions(trigger, thread, post_data)

            if not new_actions is None:
                actions = actions | new_actions

        return actions

    def handle(self, post_data, thread):
        """Actual function called to execute triggers."""
        actions = self.get_actions(thread, post_data)

        # Execute actions.
        for action in actions:
            if action[0] == 'save' and not thread.saved:
                thread.saved = True
                thread.auto_saved = True
                thread.save()

            if action[0] == 'add_tag':
                if not TagToThread.objects.filter(thread=thread, tag=action[1]).exists():
                    tag_to_thread = TagToThread(
                        thread=thread,
                        tag=action[1],
                        automatically_added=True
                    )
                    tag_to_thread.save()


class Queuer:
    """Exposes the functions which allow the threads to synchronise their wait times."""

    def __init__(self):
        self.last_api_request = None
        self.last_file_request = None

        self.total_wait = 0
        self.total_wait_time_with_lock = datetime.timedelta()

        self.file_wait_lock = threading.Lock()
        self.api_wait_lock = threading.Lock()

    def get_total_wait_time(self):
        """Get the total time for which this class forced the threads to wait."""
        return datetime.timedelta(seconds=self.total_wait)

    def get_total_wait_time_with_lock(self):
        """Get the total time for which this class forced the threads to wait plus the time waiting for the lock."""
        return self.total_wait_time_with_lock

    def wait(self, time_beetwen_requests, last_request):
        """Wait to make sure that at least time_beetwen_requests passed since last_request."""
        # Convert seconds to microseconds.
        time_beetwen_requests *= 1000000

        # Check when the last request was performed and wait if necessary.
        if not last_request is None:
            time_passed = datetime.datetime.now() - last_request

            # If not enough time passed since the last request, wait for the remaining time.
            if time_passed.microseconds < time_beetwen_requests:
                # Calculate remaining time and convert microseconds to seconds.
                wait_for_seconds = (time_beetwen_requests - time_passed.microseconds) / 1000000
                self.total_wait += wait_for_seconds
                time.sleep(wait_for_seconds)

    def api_wait(self):
        """Wait in order to satisfy the API rules."""
        wait_start = datetime.datetime.now()

        with self.api_wait_lock:
            self.wait(AppSettings.get('API_WAIT'), self.last_api_request)
            self.last_api_request = datetime.datetime.now()

        self.total_wait_time_with_lock += datetime.datetime.now() - wait_start

    def file_wait(self):
        """Wait in order to satisfy the rules. Used before downloading images."""
        wait_start = datetime.datetime.now()

        with self.file_wait_lock:
            self.wait(AppSettings.get('FILE_WAIT'), self.last_file_request)
            self.last_file_request = datetime.datetime.now()

        self.total_wait_time_with_lock += datetime.datetime.now() - wait_start


class Stats:
    """Class storing the statistics. Perfomed tasks are purely informational
    are not not a part of any other mechanic.
    """
    def __init__(self):
        self.parameters = {
            'total_download_time': datetime.timedelta(),
            'total_wait_time': datetime.timedelta(),
            'total_wait_time_with_lock': datetime.timedelta(),
            'processed_threads':  0,
            'added_posts': 0,
            'removed_posts': 0,
            'downloaded_images': 0,
            'downloaded_thumbnails': 0,
            'downloaded_threads': 0,
        }
        
        self.lock = threading.Lock()

    def add(self, name, value):
        """Add a value to the specified statistic."""
        with self.lock:
            self.parameters[name] += value

    def get(self, name):
        """Get a value of the specified statistic."""
        with self.lock:
            return self.parameters[name]

    def add_to_record(self, record, total_time, **kwargs):
        """Save the statistics in the database."""
        used_threads = kwargs.get('used_threads', AppSettings.get('SCRAPER_THREADS_NUMBER'))

        wait_time = self.get('total_wait_time_with_lock').total_seconds() / used_threads
        download_time = self.get('total_download_time').total_seconds() / used_threads

        record.total_time = total_time.total_seconds()
        record.wait_time = wait_time
        record.download_time = download_time
        record.processed_threads = self.get('processed_threads')
        record.added_posts = self.get('added_posts')
        record.removed_posts = self.get('removed_posts')
        record.downloaded_images = self.get('downloaded_images')
        record.downloaded_thumbnails = self.get('downloaded_thumbnails')
        record.downloaded_threads = self.get('downloaded_threads')

        return record

    def get_text(self, total_time, **kwargs):
        """Get the text for printing. Total processing time must be provided externally."""
        try:
            wait_percent = round(self.get('total_wait_time_with_lock').total_seconds() / total_time.total_seconds() * 100 / AppSettings.get('SCRAPER_THREADS_NUMBER'))
            downloading_percent = round(self.get('total_download_time').total_seconds() / total_time.total_seconds() * 100 / AppSettings.get('SCRAPER_THREADS_NUMBER'))

        except:
            # Division by zero. Set to 0 for the statistics.
            wait_percent = 0
            downloading_percent = 0

        return 'Time passed: %s seconds (%s%% waiting, %s%% downloading files) Processed threads: %s Added posts: %s Removed posts: %s Downloaded images: %s Downloaded thumbnails: %s Downloaded threads: %s' % (
            round(total_time.total_seconds(), 2),
            wait_percent,
            downloading_percent,
            self.get('processed_threads'),
            self.get('added_posts'),
            self.get('removed_posts'),
            self.get('downloaded_images'),
            self.get('downloaded_thumbnails'),
            self.get('downloaded_threads'),
        )

    def merge(self, stats):
        """Merge the data from other instance of this class."""
        for key in self.parameters:
            self.add(key, stats.get(key))

class Scraper:
    """Base class for the scrapers."""

    def __init__(self, board, **kwargs):
        """Board is a database object, not a board name.
        Accepted kwargs: (bool) progress, (Queuer) queuer
        """
        self.board = board
        self.stats = Stats()

        self.queuer = kwargs.get('queuer', Queuer())
        self.show_progress = kwargs.get('progress', False)
        
    def get_url(self, url):
        """Download data from an url."""
        download_start = datetime.datetime.now()
        data = requests.get(url, timeout=AppSettings.get('CONNECTION_TIMEOUT'))
        self.stats.add('total_download_time', datetime.datetime.now() - download_start)
        return data

class ThreadScraper(Scraper):
    """Scraper which scraps the data from a single thread."""

    def __init__(self, board, thread_info, **kwargs):
        super(ThreadScraper, self).__init__(board, **kwargs)
        self.thread_info = thread_info
        self.triggers = kwargs.get('triggers', Triggers())
        self.modified = False

    def get_image(self, filename, extension):
        """Download an image."""
        url = 'https://i.4cdn.org/%s/%s%s' % (self.board.name, filename, extension)
        self.queuer.file_wait()
        self.stats.add('downloaded_images', 1)
        return self.get_url(url).content

    def get_thumbnail(self, filename):
        """Download a thumbnail."""
        url = 'https://t.4cdn.org/%s/%ss.jpg' % (self.board.name, filename)
        self.queuer.file_wait()
        self.stats.add('downloaded_thumbnails', 1)
        return self.get_url(url).content

    def get_thread_json(self, thread_number):
        """Get the thread data from the official API."""
        url = 'https://a.4cdn.org/%s/thread/%s.json' % (self.board.name, thread_number)
        self.queuer.api_wait()
        self.stats.add('downloaded_threads', 1)
        return self.get_url(url).json()
    
    def get_thread_number(self):
        """Get the number of a thread scrapped by this instance."""
        return self.thread_info.number

    def should_be_updated(self, thread):
        """Determine if the thread should be updated."""
        # Should the thread be updated? Check only if there is any data about the thread
        # (the download of the first post was successful).
        if not thread.last_reply_time() is None and thread.post_set.count() > 0:
            # It has to have newer replies or different number of replies.
            # Note: use count_replies because 4chan does not count the first post as a reply.
            if (self.thread_info.last_reply_time <= thread.last_reply_time()
                and self.thread_info.replies == thread.count_replies()):
                return False

        return True

    def get_last_post_number(self, thread):
        """Determine the last post's number or pick an imaginary one.
        Only posts with a number above this one will be added to the database."""
        last_post = thread.post_set.order_by('number').last()

        if last_post is None:
            return -1;
        else:
            return last_post.number

    def add_post(self, post_data, thread):
        """Add the post."""
        # Download images first, don't waste time when in transaction.
        if not post_data.filename is None:
            try:
                image_tmp = ContentFile(self.get_image(post_data.filename, post_data.extension))
                thumbnail_tmp = ContentFile(self.get_thumbnail(post_data.filename))

                filename_image = format('%s%s' % (post_data.filename, post_data.extension))
                filename_thumbnail = format('%s%s' % (post_data.filename, '.jpg'))

            except:
                raise ScrapError('Unable to download an image. Stopping at this post.')

        # Save post in the database.
        with transaction.atomic():
            # Do not save earlier or you might end up with a thread without posts.
            if not thread.pk:
                thread.save()

            # Save post.
            post = Post.objects.create(
                thread=thread,
                number=post_data.number,
                time=post_data.time,
                name=post_data.name,
                trip=post_data.trip,
                email=post_data.email,
                country=post_data.country,
                subject=post_data.subject,
                comment=post_data.comment
            )

            # Save image.
            if not post_data.filename is None:
                image = Image(original_name=post_data.original_filename, post=post)

                image.image.save(filename_image, image_tmp)
                image.thumbnail.save(filename_thumbnail, thumbnail_tmp)

                image.save()

            self.triggers.handle(post_data, thread)

        self.stats.add('added_posts', 1)

        # Just to give something to look at. 
        # "_" is a post without an image, "-" is a post with an image
        if self.show_progress:
            print('-' if post_data.filename else '_', end="", flush=True)

    def handle_thread(self):
        """Download/update the thread if necessary."""
        # Download only above certain number of posts.
        # (seriously it is wise do let the moderators do their job first)
        if self.thread_info.replies < self.board.replies_threshold:
            return

        # Get the existing entry for this thread from the database or create a new record for it.
        # get_or_create is not used to avoid saving outside of the transaction - empty thread
        # could be created in case of an error.
        try:
            thread = Thread.objects.get(board=self.board, number=self.thread_info.number)

            if not self.should_be_updated(thread):
                return

        except Thread.DoesNotExist:
            thread = Thread(board=self.board, number=self.thread_info.number)

        # Get last post number.
        last_post_number = self.get_last_post_number(thread)

        # Download the thread data.
        try:
            thread_json = self.get_thread_json(self.thread_info.number)

        except:
            raise ScrapError('Unable to download the thread data. It might not exist anymore.')

        # Create a list for downloaded post numbers.
        # We will later check if something from our database is missing in this list and remove it.
        post_numbers = []

        try:
            # Add posts.
            for post_json in thread_json['posts']:
                # Create container class and parse info in the process.
                post_data = PostData(post_json)
                
                # Store post number.
                post_numbers.append(post_data.number)

                # Actual update.
                if post_data.number > last_post_number:
                    self.modified = True
                    self.add_post(post_data, thread)

            # Remove posts which don't exist in the thread.
            for post in thread.post_set.all():
                if not post.number in post_numbers:
                    self.stats.add('removed_posts', 1)
                    self.modified = True
                    post.delete()

        except Exception as e:
            sys.stderr.write('%s\m' % (e))
            self.modified = True


class ThreadScraperThread(ThreadScraper, threading.Thread):
    """This is simply a ThreadScraper which is supposed to run as a thread."""

    def __init__(self, board, thread_info, board_scraper, **kwargs):
        super().__init__(board, thread_info, **kwargs)
        threading.Thread.__init__(self)
        self.board_scraper = board_scraper

    def run(self):
        try:
            self.handle_thread()

        except Exception as e:
            sys.stderr.write('%s\n' % (e))

        finally:
            self.board_scraper.on_thread_scraper_done(self)

class BoardScraper(Scraper):
    """Class which basically launches and coordinates ThreadScraperThread classes."""

    def __init__(self, board, **kwargs):
        super(BoardScraper, self).__init__(board, **kwargs)

        self.triggers = Triggers()

        self.thread_gen = None
        self.running_threads = []
        self.running_threads_lock = threading.Lock()

    def get_catalog_json(self):
        """Get the catalog data from the official API."""
        url = 'https://a.4cdn.org/%s/catalog.json' % (self.board.name)
        self.queuer.api_wait()
        return self.get_url(url).json()

    def on_thread_scraper_done(self, thread_scraper):
        """Called by a child thread after it finishes working."""
        try:
            # Merge the stats from the child thread.
            self.stats.merge(thread_scraper.stats)
        
        except Exception as e:
            sys.stderr.write('%s\n' % (e))

        finally:
            # Launch a new thread in place of the one that just finished.
            self.launch_thread()

            # Remove the thread from the list of running threads.
            self.remove_running(thread_scraper.get_thread_number())

    def add_running(self, thread_number):
        """Add a thread to the list of the running threads."""
        with self.running_threads_lock:
            self.running_threads.append(thread_number)

    def remove_running(self, thread_number):
        """Remove a thread from the list of running threads."""
        with self.running_threads_lock:
            if thread_number in self.running_threads:
                self.running_threads.remove(thread_number)

    def launch_thread(self):
        """Launch a new ThreadScraperThread."""
        self.stats.add('processed_threads', 1)

        # Get a new thread from the generator.
        thread_data = self.get_thread()
        if thread_data is None:
            return

        # Prepare thread info class based on returned JSON.
        thread_info = ThreadInfo(thread_data)

        # Add a new thread to the list of all running threads.
        self.add_running(thread_info.number)

        try:
            # Launch the thread.
            thread_scraper = ThreadScraperThread(self.board, thread_info, self,
                queuer=self.queuer,
                triggers=self.triggers,
                progress=self.show_progress
            )
            thread_scraper.daemon = True
            thread_scraper.start()

        except Exception as e:
            # Remove the thread from the list of all running threads.
            self.remove_running(thread_info.number)
            sys.stderr.write('%s\m' % (e))

    def get_thread(self):
        """Get the next thread."""
        if self.thread_gen is None:
            self.thread_gen = self.thread_generator()

        try:
            return next(self.thread_gen)

        except StopIteration:
            return None

    def thread_generator(self):
        """Generator for the thread JSON."""
        for page in self.catalog:
            for thread in page['threads']:
                yield thread

    def update(self):
        """Call this to update the database."""
        # Get the catalog from the API.
        try:
            self.catalog = self.get_catalog_json()

        except:
            raise ScrapError('Unable to download or parse the catalog data. Board update stopped.')

        # Launch the initial threads. Next ones will be launched automatically.
        for i in range(0, AppSettings.get('SCRAPER_THREADS_NUMBER')):
            self.launch_thread()

        # Wait for all threads to finish.
        while True:
            time.sleep(1)
            with self.running_threads_lock:
                if not len(self.running_threads):
                    break

        self.stats.add('total_wait_time', self.queuer.get_total_wait_time())
        self.stats.add('total_wait_time_with_lock', self.queuer.get_total_wait_time_with_lock())
