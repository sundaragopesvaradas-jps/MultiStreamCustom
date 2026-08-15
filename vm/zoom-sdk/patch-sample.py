#!/usr/bin/env python3
"""Adapt Zoom's raw-recording sample for Multistream.

Only meeting_sdk_demo.cpp and CMakeLists.txt are patched here; the raw audio and
video writers are replaced outright from vm/zoom-sdk/src.

Every edit asserts that its anchor was found. A silently skipped patch produces a
bot that joins and records nothing, which is far more expensive to diagnose than
a failed build.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Subscribe to a participant who actually has a camera on. The sample takes the
# first entry in the participant list, which is usually the bot itself.
GET_USER_ID = """uint32_t getUserID() {
\tm_pParticipantsController = m_pMeetingService->GetMeetingParticipantsController();
\tIList<unsigned int>* list = m_pParticipantsController->GetParticipantsList();
\tif (!list || list->GetCount() == 0) {
\t\tstd::cout << "No participants yet" << std::endl;
\t\treturn 0;
\t}
\tunsigned int myself = 0;
\tIUserInfo* me = m_pParticipantsController->GetMySelfUser();
\tif (me) {
\t\tmyself = me->GetUserID();
\t}
\tunsigned int fallback = 0;
\tfor (int i = 0; i < list->GetCount(); i++) {
\t\tunsigned int uid = list->GetItem(i);
\t\tif (uid == myself) {
\t\t\tcontinue;
\t\t}
\t\tIUserInfo* info = m_pParticipantsController->GetUserByUserID(uid);
\t\tif (info && info->IsVideoOn()) {
\t\t\tstd::cout << "Subscribing to video of user " << uid << std::endl;
\t\t\treturn uid;
\t\t}
\t\tif (fallback == 0) {
\t\t\tfallback = uid;
\t\t}
\t}
\tif (fallback != 0) {
\t\tstd::cout << "No camera on yet; subscribing to user " << fallback << std::endl;
\t\treturn fallback;
\t}
\tstd::cout << "Only self in meeting; using own user id" << std::endl;
\treturn myself;
}"""

# Raw audio cannot be subscribed to before the bot is on the VoIP channel;
# without this the SDK returns SDKERR_NOT_JOIN_AUDIO (32) and audio is silent.
JOIN_VOIP = """IMeetingAudioController* audioCtrl = m_pMeetingService->GetMeetingAudioController();
\t\t\t\t\tif (audioCtrl) {
\t\t\t\t\t\tSDKError joined = audioCtrl->JoinVoip();
\t\t\t\t\t\tstd::cout << "JoinVoip result: " << joined << std::endl;
\t\t\t\t\t\tfor (int attempt = 0; attempt < 10 && joined != SDKERR_SUCCESS; attempt++) {
\t\t\t\t\t\t\tsleep(1);
\t\t\t\t\t\t\tjoined = audioCtrl->JoinVoip();
\t\t\t\t\t\t\tstd::cout << "JoinVoip retry " << attempt << " result: " << joined << std::endl;
\t\t\t\t\t\t}
\t\t\t\t\t}
\t\t\t\t\taudioHelper = GetAudioRawdataHelper();"""

CMAKE_SWSCALE = """
# Multistream: the raw video writer letterboxes every frame to a fixed size.
pkg_check_modules(SWSCALE REQUIRED libswscale)
target_include_directories(meetingSDKDemo PRIVATE ${SWSCALE_INCLUDE_DIRS})
target_link_directories(meetingSDKDemo PRIVATE ${SWSCALE_LIBRARY_DIRS})
target_link_libraries(meetingSDKDemo ${SWSCALE_LIBRARIES})
"""


class PatchError(Exception):
    pass


def replace_once(text: str, needle: str, value: str, *, what: str) -> str:
    if needle not in text:
        raise PatchError(f"anchor for {what} not found: {needle[:70]!r}")
    return text.replace(needle, value, 1)


def replace_all(text: str, needle: str, value: str, *, what: str) -> str:
    if needle not in text:
        raise PatchError(f"anchor for {what} not found: {needle[:70]!r}")
    return text.replace(needle, value)


def patch_demo(path: Path) -> None:
    cpp = path.read_text(encoding="utf-8", errors="replace")

    # Read the bot's name from config.txt instead of the sample's "LinuxChun".
    if "display_name=config" not in cpp:
        declared = False
        for declaration in (
            "std::string meeting_number, token, meeting_password, recording_token,onBehalfOf_Token;",
            "std::string meeting_number, token, meeting_password, recording_token;",
        ):
            if declaration in cpp:
                cpp = cpp.replace(
                    declaration, declaration[:-1] + ", display_name;", 1
                )
                declared = True
                break
        if not declared:
            raise PatchError("could not find the config string declarations")
        cpp = replace_once(
            cpp,
            'if (config.find("recording_token") != config.end())',
            'if (config.find("display_name") != config.end()) {\n'
            "\t\tdisplay_name=config[\"display_name\"];\n"
            '\t\tstd::cout << "display_name: " << display_name << std::endl;\n'
            "\t}\n"
            '\tif (config.find("recording_token") != config.end())',
            what="display_name parsing",
        )

    cpp = replace_once(
        cpp,
        'withoutloginParam.userName = "LinuxChun";',
        "withoutloginParam.userName = display_name.empty() "
        '? "ISKCON Deoghar Archive" : display_name.c_str();',
        what="bot display name",
    )

    # Join with camera off on every join path. Audio must stay on, otherwise the
    # SDK refuses the raw audio subscription with SDKERR_NOT_JOIN_AUDIO.
    if "withoutloginParam.isVideoOff = false;" in cpp:
        cpp = replace_all(
            cpp,
            "withoutloginParam.isVideoOff = false;",
            "withoutloginParam.isVideoOff = true;",
            what="join with video off",
        )

    # Sample bug: `!recording_token.size() == 0` is always false, so the local
    # recording token was never passed and the bot had no recording rights.
    cpp = replace_once(
        cpp,
        "if (!recording_token.size() == 0)",
        "if (recording_token.size() != 0)",
        what="recording token check",
    )

    # Meeting SDK 7.x added a FrameDataFormat argument.
    cpp = replace_once(
        cpp,
        "SDKError err = p_videoSourceHelper->setExternalVideoSource(virtual_camera_video_source);",
        "SDKError err = p_videoSourceHelper->setExternalVideoSource("
        "virtual_camera_video_source, FrameDataFormat_I420_FULL);",
        what="setExternalVideoSource for SDK 7.x",
    )

    if "JoinVoip result" not in cpp:
        cpp = replace_once(
            cpp,
            "audioHelper = GetAudioRawdataHelper();",
            JOIN_VOIP,
            what="JoinVoip before audio subscribe",
        )

    if "IsVideoOn()" not in cpp:
        cpp, count = re.subn(
            r"uint32_t getUserID\(\)\s*\{.*?\n\}", GET_USER_ID, cpp, count=1, flags=re.S
        )
        if count != 1:
            raise PatchError("could not find getUserID() to replace")

    path.write_text(cpp, encoding="utf-8")


def patch_cmake(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    if "SWSCALE" in text:
        return
    if "add_executable(meetingSDKDemo" not in text:
        raise PatchError("CMakeLists has no meetingSDKDemo target")
    path.write_text(text.rstrip() + "\n" + CMAKE_SWSCALE, encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch-sample.py /path/to/sample/demo", file=sys.stderr)
        return 2
    demo = Path(sys.argv[1])
    try:
        patch_demo(demo / "meeting_sdk_demo.cpp")
        patch_cmake(demo / "CMakeLists.txt")
    except PatchError as exc:
        print(f"PATCH FAILED: {exc}", file=sys.stderr)
        return 1
    print("sample patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
