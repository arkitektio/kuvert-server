"""Schema-surface checks: enums in step with the models."""

import pytest

from mail import enums, models


@pytest.mark.parametrize(
    "enum,choices",
    [
        (enums.MailErrorCode, models.MailErrorCode),
        (enums.Provider, models.Provider),
        (enums.Protocol, models.Protocol),
        (enums.Security, models.Security),
        (enums.AuthMethod, models.AuthMethod),
        (enums.MailAccountStatus, models.MailAccountStatus),
        (enums.Visibility, models.Visibility),
        (enums.FolderRole, models.FolderRole),
        (enums.OutgoingStatus, models.OutgoingStatus),
        (enums.OAuthLinkStatus, models.OAuthLinkStatus),
        (enums.TaskStatus, models.TaskStatus),
        (enums.TaskLinkSource, models.TaskLinkSource),
    ],
)
def test_enums_match_choices(enum, choices):
    assert [member.value for member in enum] == list(choices.values)
