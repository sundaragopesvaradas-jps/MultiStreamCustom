// Shared start signal for the raw audio and video writers.
//
// Both streams are written to live encoder pipes with no timestamps, so the
// only thing keeping them in sync is that they begin at the same instant.
// Audio is the master clock: the first mixed-audio buffer opens the gate and
// the video pacer starts emitting from that moment.

#ifndef MULTISTREAM_SYNC_H
#define MULTISTREAM_SYNC_H

#include <chrono>
#include <condition_variable>
#include <mutex>

namespace multistream {

class StartGate {
public:
	static StartGate& instance() {
		static StartGate gate;
		return gate;
	}

	void signal() {
		{
			std::lock_guard<std::mutex> guard(mutex_);
			if (started_) {
				return;
			}
			started_ = true;
		}
		condition_.notify_all();
	}

	// Falls through after the timeout so a meeting that never delivers audio
	// still produces a video track instead of hanging forever.
	void wait(int timeoutSeconds) {
		std::unique_lock<std::mutex> guard(mutex_);
		condition_.wait_for(guard, std::chrono::seconds(timeoutSeconds),
		                    [this] { return started_; });
	}

private:
	StartGate() : started_(false) {}

	bool started_;
	std::mutex mutex_;
	std::condition_variable condition_;
};

}  // namespace multistream

#endif  // MULTISTREAM_SYNC_H
