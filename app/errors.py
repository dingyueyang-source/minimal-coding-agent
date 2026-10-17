class AgentError(Exception):
    def __init__(self, code, message, *, timeout=False):
        super().__init__(message)
        self.code = code
        self.timeout = timeout


class AdmissionError(AgentError):
    def __init__(self, status_code, code, message):
        super().__init__(code, message)
        self.status_code = status_code
