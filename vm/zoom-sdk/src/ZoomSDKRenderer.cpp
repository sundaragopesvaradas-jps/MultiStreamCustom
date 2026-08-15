// Multistream replacement for the Zoom sample's raw video writer.
//
// The sample appended every frame to output.yuv and muxed it after the meeting.
// That cost ~20 MB/s of disk and minutes of encoding once the call ended. This
// writer feeds a live encoder instead, so the byte stream handed to ffmpeg has
// to be well-formed at all times:
//
//   * Zoom changes resolution mid-call as bandwidth varies, so every frame is
//     letterboxed into one fixed size.
//   * Frames arrive at an irregular rate, so a pacing thread emits exactly
//     MULTISTREAM_VIDEO_FPS frames per second, repeating the last frame (black
//     before the first one). A constant rate starting at the audio clock is
//     what keeps the two tracks aligned without any timestamp negotiation.

#include "rawdata/rawdata_video_source_helper_interface.h"
#include "ZoomSDKRenderer.h"
#include "zoom_sdk_def.h"
#include "MultistreamSync.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

extern "C" {
#include <libswscale/swscale.h>
}

namespace {

// I420 black in limited range.
const unsigned char kBlackY = 16;
const unsigned char kBlackUV = 128;

// How long the pacer waits for the audio clock before starting on its own.
const int kAudioWaitSeconds = 30;

int env_int(const char* name, int fallback) {
	const char* raw = std::getenv(name);
	if (raw == NULL || *raw == '\0') {
		return fallback;
	}
	int value = std::atoi(raw);
	return value > 0 ? value : fallback;
}

int even(int value) {
	return value & ~1;
}

class LiveVideoWriter {
public:
	static LiveVideoWriter& instance() {
		static LiveVideoWriter writer;
		return writer;
	}

	// Called on the SDK's video thread, so it must never block on the pipe.
	void submit(YUVRawDataI420* data) {
		int srcW = data->GetStreamWidth();
		int srcH = data->GetStreamHeight();
		if (srcW <= 0 || srcH <= 0) {
			return;
		}
		if (!scaleInto(data, srcW, srcH, incoming_)) {
			return;
		}
		std::lock_guard<std::mutex> guard(mutex_);
		incoming_.swap(shared_);
		haveFrame_ = true;
	}

private:
	LiveVideoWriter()
		: width_(even(env_int("MULTISTREAM_VIDEO_WIDTH", 1280))),
		  height_(even(env_int("MULTISTREAM_VIDEO_HEIGHT", 720))),
		  fps_(env_int("MULTISTREAM_VIDEO_FPS", 15)),
		  frameBytes_(0),
		  sws_(NULL),
		  swsW_(0),
		  swsH_(0),
		  swsInnerW_(0),
		  swsInnerH_(0),
		  haveFrame_(false) {
		frameBytes_ = static_cast<size_t>(width_) * height_ * 3 / 2;
		incoming_.resize(frameBytes_);
		shared_.resize(frameBytes_);
		outgoing_.resize(frameBytes_);
		blacken(incoming_);
		blacken(shared_);
		blacken(outgoing_);

		const char* path = std::getenv("MULTISTREAM_VIDEO_OUT");
		path_ = (path != NULL && *path != '\0') ? path : "output.yuv";
		std::cout << "video writer: " << width_ << "x" << height_ << " @ "
		          << fps_ << "fps -> " << path_ << std::endl;
		thread_ = std::thread(&LiveVideoWriter::pace, this);
		thread_.detach();
	}

	void blacken(std::vector<unsigned char>& buffer) {
		size_t ySize = static_cast<size_t>(width_) * height_;
		std::memset(&buffer[0], kBlackY, ySize);
		std::memset(&buffer[ySize], kBlackUV, buffer.size() - ySize);
	}

	// Fits the source frame inside the output, centred, preserving aspect.
	bool scaleInto(YUVRawDataI420* data, int srcW, int srcH,
	               std::vector<unsigned char>& dst) {
		double factor = std::min(static_cast<double>(width_) / srcW,
		                         static_cast<double>(height_) / srcH);
		int innerW = even(static_cast<int>(srcW * factor));
		int innerH = even(static_cast<int>(srcH * factor));
		if (innerW <= 0 || innerH <= 0) {
			return false;
		}
		if (sws_ == NULL || srcW != swsW_ || srcH != swsH_ ||
		    innerW != swsInnerW_ || innerH != swsInnerH_) {
			if (sws_ != NULL) {
				sws_freeContext(sws_);
			}
			sws_ = sws_getContext(srcW, srcH, AV_PIX_FMT_YUV420P,
			                      innerW, innerH, AV_PIX_FMT_YUV420P,
			                      SWS_BILINEAR, NULL, NULL, NULL);
			swsW_ = srcW;
			swsH_ = srcH;
			swsInnerW_ = innerW;
			swsInnerH_ = innerH;
			std::cout << "video writer: scaling " << srcW << "x" << srcH
			          << " -> " << innerW << "x" << innerH << std::endl;
		}
		if (sws_ == NULL) {
			return false;
		}

		blacken(dst);
		int x0 = even((width_ - innerW) / 2);
		int y0 = even((height_ - innerH) / 2);
		size_t ySize = static_cast<size_t>(width_) * height_;
		size_t uvSize = ySize / 4;
		int chromaW = width_ / 2;

		const unsigned char* srcData[3];
		srcData[0] = reinterpret_cast<const unsigned char*>(data->GetYBuffer());
		srcData[1] = reinterpret_cast<const unsigned char*>(data->GetUBuffer());
		srcData[2] = reinterpret_cast<const unsigned char*>(data->GetVBuffer());
		int srcStride[3] = {srcW, srcW / 2, srcW / 2};

		unsigned char* dstData[3];
		dstData[0] = &dst[static_cast<size_t>(y0) * width_ + x0];
		dstData[1] = &dst[ySize + static_cast<size_t>(y0 / 2) * chromaW + x0 / 2];
		dstData[2] = &dst[ySize + uvSize + static_cast<size_t>(y0 / 2) * chromaW + x0 / 2];
		int dstStride[3] = {width_, chromaW, chromaW};

		sws_scale(sws_, srcData, srcStride, 0, srcH, dstData, dstStride);
		return true;
	}

	// Opening a FIFO blocks until the encoder attaches, so it happens on this
	// thread rather than on an SDK callback.
	void pace() {
		FILE* out = std::fopen(path_.c_str(), "wb");
		if (out == NULL) {
			std::cout << "video writer: cannot open " << path_ << std::endl;
			return;
		}
		multistream::StartGate::instance().wait(kAudioWaitSeconds);
		std::chrono::steady_clock::time_point start = std::chrono::steady_clock::now();
		long long emitted = 0;
		while (true) {
			std::chrono::nanoseconds offset(emitted * 1000000000LL / fps_);
			std::this_thread::sleep_until(start + offset);
			{
				std::lock_guard<std::mutex> guard(mutex_);
				if (haveFrame_) {
					shared_.swap(outgoing_);
					haveFrame_ = false;
				}
			}
			if (std::fwrite(&outgoing_[0], 1, frameBytes_, out) != frameBytes_) {
				std::cout << "video writer: pipe closed after " << emitted
				          << " frames" << std::endl;
				break;
			}
			emitted++;
		}
		std::fclose(out);
	}

	int width_;
	int height_;
	int fps_;
	size_t frameBytes_;
	std::string path_;
	SwsContext* sws_;
	int swsW_;
	int swsH_;
	int swsInnerW_;
	int swsInnerH_;
	bool haveFrame_;
	std::mutex mutex_;
	std::vector<unsigned char> incoming_;
	std::vector<unsigned char> shared_;
	std::vector<unsigned char> outgoing_;
	std::thread thread_;
};

// The pacer has to be running before the first frame arrives, otherwise the
// video track would start late and lag the audio by however long it took
// someone to turn a camera on.
struct EagerStart {
	EagerStart() {
		if (std::getenv("MULTISTREAM_VIDEO_OUT") != NULL) {
			LiveVideoWriter::instance();
		}
	}
};

EagerStart eager_start;

}  // namespace

void ZoomSDKRenderer::onRawDataFrameReceived(YUVRawDataI420* data) {
	LiveVideoWriter::instance().submit(data);
}

void ZoomSDKRenderer::onRawDataStatusChanged(RawDataStatus status) {
	std::cout << "onRawDataStatusChanged: " << status << std::endl;
}

void ZoomSDKRenderer::onRendererBeDestroyed() {
	std::cout << "onRendererBeDestroyed" << std::endl;
}

void ZoomSDKRenderer::SaveToRawYUVFile(YUVRawDataI420* data) {
	LiveVideoWriter::instance().submit(data);
}
