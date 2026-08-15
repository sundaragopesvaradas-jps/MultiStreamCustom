// Multistream replacement for the Zoom sample's raw audio writer.
//
// The sample reopened the output file on every 10 ms callback and echoed the
// PCM bytes to stdout, which flooded the log. This keeps one handle open for
// the whole meeting and writes straight into the live encoder pipe.
//
// The first buffer also starts the shared capture clock, which the video pacer
// waits on so the two tracks begin together.

#include "rawdata/rawdata_audio_helper_interface.h"
#include "ZoomSDKAudioRawData.h"
#include "zoom_sdk_def.h"
#include "MultistreamSync.h"

#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <string>

namespace {

// Must match what the wrapper tells ffmpeg to expect.
const unsigned int kExpectedSampleRate = 32000;
const unsigned int kExpectedChannels = 1;

FILE* audio_out = NULL;
bool audio_failed = false;

FILE* open_audio_output() {
	const char* raw = std::getenv("MULTISTREAM_AUDIO_OUT");
	std::string path = (raw != NULL && *raw != '\0') ? raw : "audio.pcm";
	FILE* handle = std::fopen(path.c_str(), "wb");
	if (handle == NULL) {
		std::cout << "audio writer: cannot open " << path << std::endl;
	} else {
		std::cout << "audio writer: -> " << path << std::endl;
	}
	return handle;
}

}  // namespace

void ZoomSDKAudioRawData::onOneWayAudioRawDataReceived(AudioRawData* audioRawData, uint32_t node_id)
{
}

void ZoomSDKAudioRawData::onMixedAudioRawDataReceived(AudioRawData* audioRawData)
{
	if (audio_failed || audioRawData == NULL) {
		return;
	}
	if (audio_out == NULL) {
		unsigned int rate = audioRawData->GetSampleRate();
		unsigned int channels = audioRawData->GetChannelNum();
		std::cout << "audio writer: " << rate << "Hz " << channels << "ch" << std::endl;
		if (rate != kExpectedSampleRate || channels != kExpectedChannels) {
			std::cout << "audio writer: WARNING expected " << kExpectedSampleRate
			          << "Hz " << kExpectedChannels
			          << "ch — recording will play at the wrong speed" << std::endl;
		}
		audio_out = open_audio_output();
		if (audio_out == NULL) {
			audio_failed = true;
			return;
		}
		// Tells the wrapper the capture actually started. Without audio the
		// encoder cannot get past probing its first input, so the wrapper
		// treats a missing sentinel as a failed recording rather than waiting.
		const char* sentinel = std::getenv("MULTISTREAM_AUDIO_READY");
		if (sentinel != NULL && *sentinel != '\0') {
			FILE* marker = std::fopen(sentinel, "w");
			if (marker != NULL) {
				std::fclose(marker);
			}
		}
		multistream::StartGate::instance().signal();
	}

	unsigned int length = audioRawData->GetBufferLen();
	if (length == 0) {
		return;
	}
	if (std::fwrite(audioRawData->GetBuffer(), 1, length, audio_out) != length) {
		std::cout << "audio writer: pipe closed" << std::endl;
		std::fclose(audio_out);
		audio_out = NULL;
		audio_failed = true;
	}
}

void ZoomSDKAudioRawData::onShareAudioRawDataReceived(AudioRawData* data_, uint32_t user_id)
{
}

void ZoomSDKAudioRawData::onOneWayInterpreterAudioRawDataReceived(AudioRawData* data_, const zchar_t* pLanguageName)
{
}
