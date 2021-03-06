from django.contrib.auth.models import AnonymousUser
from django.conf import settings
from django.db import IntegrityError

from experiments.models import Enrollment, CONTROL_GROUP, ENABLED_STATE, CONTROL_STATE, Experiment
from experiments.counters import counter_increment
from experiments.manager import experiment_manager
from gargoyle.manager import gargoyle
from experiments import signals

import re

PARTICIPANT_KEY = '%s:%s:participant'
GOAL_KEY = '%s:%s:%s:goal'

# Known bots user agents to drop from experiments
BOT_REGEX = re.compile("(Baidu|Gigabot|Googlebot|YandexBot|AhrefsBot|TVersity|libwww-perl|Yeti|lwp-trivial|msnbot|bingbot|facebookexternalhit|Twitterbot|Twitmunin|SiteUptime|TwitterFeed|Slurp|WordPress|ZIBB|ZyBorg)", re.IGNORECASE)

def record_goal(request, goal_name):
    experiment_user = WebUser(request)
    experiment_user.record_goal(goal_name)

def sync_experiments(sender, request, user, **kwargs):
    """
    Syncs experiment data between the session and the db
    Attached to user_logged_in signal.
    """

    # get session data
    sessions_experiments = request.session.get('experiments_enrollments', {})

    # if non anon, get db data
    try:
        db_experiments = dict((enrollment.experiment.name, (enrollment.alternative, enrollment.goals)) for enrollment in Enrollment.objects.filter(user=user))
    except Enrollment.DoesNotExist:
        db_experiments = {}

    # return here if they're the same and no updating needs to be done
    if sessions_experiments == db_experiments:
        return

    # merge data - settings define whether recent or db entries get priority. Should depend on how you store sessions. Persistent sessions between login/out are best for this (Cookies ideal as preserve between browser sessions)
    synced_experiments = {}
    if getattr(settings, 'EXPERIMENTS_PRIORITY', 'db') == 'db':
        synced_experiments = dict(sessions_experiments.items() + db_experiments.items())

    # TODO decide if there is a use case for this - think not in reality
    elif getattr(settings, 'EXPERIMENTS_PRIORITY', 'db') == 'recent':
        # iterate merged list of db and sessions experiments
        for key, (alternative, goals, date) in sessions_experiments.iteritems() + db_experiments.iteritems():

            # add to dict if not already in, otherwise compare dates
            if key not in synced_experiments:
                synced_experiments[key] = (alternative, goals, date)
            else:
                _alternative, _goals, _date = synced_experiments[key]
                if date > _date:
                    synced_experiments[key] = (alternative, goals, date)

    # save to session - doesn't work anymore as not attached to WebUser
    #self.session['experiments_enrollments'] = synced_experiments

    # save to db
    for key, (alternative, goals) in synced_experiments.iteritems():
        try:
            enrollment, _ = Enrollment.objects.get_or_create(user=user, experiment=Experiment.objects.get(name=key), defaults={'alternative':alternative, 'goals':goals})
        except IntegrityError, exc:
            # Already registered (db race condition under high load)
            continue

        # Update alternative if it doesn't match
        if enrollment.alternative != alternative:
            enrollment.alternative = alternative
            enrollment.save()


class WebUser(object):
    """
    Wrapper class that implements an 'ExperimentUser' object from a web request.
    """
    def __init__(self, request):
        self.request = request
        self.user = request.user
        self.session = request.session

    def is_anonymous(self):
        return self.user.is_anonymous()

    def get_registered_user(self):
        if self.is_anonymous():
            return None
        return self.user

    def is_bot(self):
        return bool(BOT_REGEX.search(self.request.META.get("HTTP_USER_AGENT","")))

    def is_verified_human(self):
        if getattr(settings, 'EXPERIMENTS_VERIFY_HUMAN', True):
            return self.session.get('experiments_verified_human', False)
        else:
            return True

    def increment_participant_count(self, experiment, alternative_name):
        # Increment experiment_name:alternative:participant counter
        counter_key = PARTICIPANT_KEY % (experiment.name, alternative_name)
        count = counter_increment(counter_key)

        signals.experiment_incr_participant.send(
            sender=self,
            request=self.request,
            experiment=experiment,
            alternative=alternative_name,
            user=self.user,
            participants=count,
        )  

    def increment_goal_count(self, experiment, alternative_name, goal_name):
        # Increment experiment_name:alternative:participant counter
        counter_key = GOAL_KEY % (experiment.name, alternative_name, goal_name)
        count = counter_increment(counter_key)

        signals.goal_hit.send(
            sender=self,
            request=self.request,
            experiment=experiment,
            alternative=alternative_name,
            goal=goal_name,
            user=self.user,
            hits=count,
        )   

    def confirm_human(self):
        self.session['experiments_verified_human'] = True

        signals.user_confirmed_human.send(
            sender=self,
            request=self.request,
            user=self.user,
        )

        enrollments = self.session.get('experiments_enrollments', None)
        if not enrollments:
            return

        # Promote experiments
        for experiment_name, data in enrollments.items():
            alternative, goals = data
            # Increment experiment_name:alternative:participant counter
            self.increment_participant_count(experiment_manager[experiment_name], alternative)

    @property
    def experiments(self):
        # Bot/Spider, so send back control group
        if self.is_bot():
            return {}

        # Registered User
        elif not self.is_anonymous():
            try:
                db_experiments = dict((enrollment.experiment.name, (enrollment.alternative, enrollment.goals, enrollment.enrollment_date)) for enrollment in Enrollment.objects.filter(user=self.get_registered_user()))
            except Enrollment.DoesNotExist:
                db_experiments = {}
            return db_experiments

        # Not registered, use sessions
        else:
            return self.session['experiments_enrollments']

    def get_enrollment(self, experiment):
        if self.is_bot():
            # Bot/Spider, so send back control group
            return CONTROL_GROUP
        if not self.is_anonymous():
            # Registered User
            try:
                return Enrollment.objects.get(user=self.get_registered_user(), experiment=experiment).alternative
            except Enrollment.DoesNotExist:
                return None
        else:
            # Not registered, use Sessions
            enrollments = self.session.get('experiments_enrollments', None)
            if enrollments and experiment.name in enrollments:
                alternative, goals = enrollments.get(experiment.name, None)
                return alternative
            return None

    def set_enrollment(self, experiment, alternative):
        if self.is_bot():
            # Bot/Spider, so don't enroll
            return
        if not self.is_anonymous():
            # Registered User
            try:
                enrollment, _ = Enrollment.objects.get_or_create(user=self.get_registered_user(), experiment=experiment, defaults={'alternative':alternative})
            except IntegrityError, exc:
                # Already registered (db race condition under high load)
                return
            # Update alternative if it doesn't match
            if enrollment.alternative != alternative:
                enrollment.alternative = alternative
                enrollment.save()

            # Increment experiment_name:alternative:participant counter
            self.increment_participant_count(experiment, alternative)
        else:
            # Not registered use Sessions
            enrollments = self.session.get('experiments_enrollments', {})
            enrollments[experiment.name] = (alternative, [])
            self.session['experiments_enrollments'] = enrollments
            # Only Increment participant count for verified users
            if self.is_verified_human():
                # Increment experiment_name:alternative:participant counter
                self.increment_participant_count(experiment, alternative)

        signals.experiment_user_added.send(
            sender=self,
            request=self.request,
            experiment=experiment,
            user=self.user,
            alternative=alternative,
        )    

    def should_increment(self, enrollment_dict, goal_name):
        # Increments goal if enabled+gargoyle switch conditions met or just enabled
        # Goal uniquely incremented for all enrollments at once

        if enrollment_dict['experiment'].state == CONTROL_STATE:
            # Control state, experiment not running
            return False
        elif enrollment_dict['experiment'].state == ENABLED_STATE and enrollment_dict['experiment'].switch_key:
            # Gargoyle state only increment actives.
            if gargoyle.is_active(enrollment_dict['experiment'].switch_key, self.request):
                if goal_name not in enrollment_dict['goals']: # Check if already recorded for this enrollment
                    return True

        if goal_name not in enrollment_dict['goals']: # Check if already recorded for this enrollment
            return True
        return False

    # Checks if the goal should be incremented
    def check_and_increment(self, enrollment_dict, goal_name):
        if self.should_increment(enrollment_dict, goal_name):
            self.increment_goal_count(enrollment_dict['experiment'], enrollment_dict['alternative'], goal_name)
            enrollment_dict['goals'].append(goal_name)
            return enrollment_dict
        return enrollment_dict

    def record_goal(self, goal_name):
        # Bots don't register goals
        if self.is_bot():
            return
        # If the user is registered
        if not self.is_anonymous():
            enrollments = Enrollment.objects.filter(user=self.get_registered_user())
            if not enrollments:
                return
            for enrollment in enrollments: # Looks up by PK so no point caching.
                enrollment_dict = self.check_and_increment(enrollment.to_dict(), goal_name)
                enrollment.goals = enrollment_dict["goals"]
                enrollment.save()
            return
        # If confirmed human
        if self.is_verified_human():
            enrollments = self.session.get('experiments_enrollments', None)
            new_enrollments = enrollments
            if not enrollments:
                return
            for experiment_name, data in enrollments.items():
                alternative, goals = data
                enrollment_dict = {
                    "experiment": experiment_manager[experiment_name],
                    "alternative": alternative,
                    "goals": goals,
                }
                new_enrollment_dict = self.check_and_increment(enrollment_dict, goal_name)
                new_enrollments[experiment_name] = (new_enrollment_dict['alternative'], new_enrollment_dict['goals'])

            self.session['experiments_enrollments'] = new_enrollments
            return
        else:
            # TODO: store temp goals and convert later when is_human is triggered
            # if verify human is called quick enough this should rarely happen.
            pass

class StaticUser(WebUser):
    def __init__(self):
        self.request = None
        self.user = AnonymousUser()
        self.session = {}