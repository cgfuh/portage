# Copyright 1999-2018 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2

import logging

from portage import os
from portage.util import writemsg_level
from _emerge.AbstractPollTask import AbstractPollTask
import signal
import errno

class SubProcess(AbstractPollTask):

	__slots__ = ("pid",) + \
		("_dummy_pipe_fd", "_files", "_reg_id", "_waitpid_id")

	# This is how much time we allow for waitpid to succeed after
	# we've sent a kill signal to our subprocess.
	_cancel_timeout = 1000 # 1 second

	def _poll(self):
		if self.returncode is not None:
			return self.returncode
		if self.pid is None:
			return self.returncode
		if self._registered:
			return self.returncode

		try:
			# With waitpid and WNOHANG, only check the
			# first element of the tuple since the second
			# element may vary (bug #337465).
			retval = os.waitpid(self.pid, os.WNOHANG)
		except OSError as e:
			if e.errno != errno.ECHILD:
				raise
			del e
			retval = (self.pid, 1)

		if retval[0] == 0:
			return None
		self._set_returncode(retval)
		self._async_wait()
		return self.returncode

	def _cancel(self):
		if self.isAlive():
			try:
				os.kill(self.pid, signal.SIGTERM)
			except OSError as e:
				if e.errno == errno.EPERM:
					# Reported with hardened kernel (bug #358211).
					writemsg_level(
						"!!! kill: (%i) - Operation not permitted\n" %
						(self.pid,), level=logging.ERROR,
						noiselevel=-1)
				elif e.errno != errno.ESRCH:
					raise

	def isAlive(self):
		return self.pid is not None and \
			self.returncode is None

	def _async_waitpid(self):
		"""
		Wait for exit status of self.pid asynchronously, and then
		set the returncode and notify exit listeners. This is
		prefered over _waitpid_loop, since the synchronous nature
		of _waitpid_loop can cause event loop recursion.
		"""
		if self.returncode is not None:
			self._async_wait()
		elif self._waitpid_id is None:
			self._waitpid_id = self.scheduler.child_watch_add(
				self.pid, self._async_waitpid_cb)

	def _async_waitpid_cb(self, pid, condition, user_data=None):
		if pid != self.pid:
			raise AssertionError("expected pid %s, got %s" % (self.pid, pid))
		self._set_returncode((pid, condition))
		self._async_wait()

	def _waitpid_cb(self, pid, condition, user_data=None):
		if pid != self.pid:
			raise AssertionError("expected pid %s, got %s" % (self.pid, pid))
		self._set_returncode((pid, condition))

	def _orphan_process_warn(self):
		pass

	def _unregister(self):
		"""
		Unregister from the scheduler and close open files.
		"""

		self._registered = False

		if self._reg_id is not None:
			self.scheduler.source_remove(self._reg_id)
			self._reg_id = None

		if self._waitpid_id is not None:
			self.scheduler.source_remove(self._waitpid_id)
			self._waitpid_id = None

		if self._files is not None:
			for f in self._files.values():
				if isinstance(f, int):
					os.close(f)
				else:
					f.close()
			self._files = None

	def _unregister_if_appropriate(self, event):
		"""
		Override the AbstractPollTask._unregister_if_appropriate method to
		call _async_waitpid instead of wait(), so that event loop recursion
		is not triggered when the pid exit status is not yet available.
		"""
		if self._registered:
			if event & self._exceptional_events:
				self._log_poll_exception(event)
				self._unregister()
				self.cancel()
				self._async_waitpid()
			elif event & self.scheduler.IO_HUP:
				self._unregister()
				self._async_waitpid()

	def _set_returncode(self, wait_retval):
		"""
		Set the returncode in a manner compatible with
		subprocess.Popen.returncode: A negative value -N indicates
		that the child was terminated by signal N (Unix only).
		"""
		self._unregister()

		pid, status = wait_retval

		if os.WIFSIGNALED(status):
			retval = - os.WTERMSIG(status)
		else:
			retval = os.WEXITSTATUS(status)

		self.returncode = retval

