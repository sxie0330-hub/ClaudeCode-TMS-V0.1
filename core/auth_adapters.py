from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect

from .models import Profile, Role


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # No local (password) sign-up; accounts come from Google only.
        return False


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return True

    def pre_social_login(self, request, sociallogin):
        email = (sociallogin.user.email or "").lower()
        domain = email.rsplit("@", 1)[-1]
        allowed = settings.TMS_ALLOWED_EMAIL_DOMAINS
        if allowed and domain not in allowed:
            messages.error(request, f"只允许公司 Google Workspace 账号登录（{', '.join(allowed)}）。")
            raise ImmediateHttpResponse(redirect(settings.LOGIN_URL))

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        profile, _ = Profile.objects.get_or_create(user=user)
        if user.email.lower() in settings.TMS_BOOTSTRAP_ADMINS and not profile.role:
            profile.role = Role.MANAGEMENT
            profile.save()
        return user
