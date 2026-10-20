class Error(Exception):
    def __init__(self, code, message, exit_code=5, **details):
        super().__init__(message)
        self.code, self.exit_code, self.details = code, exit_code, details

    def report(self):
        return {"schemaVersion": 1, "status": "failed", "reasonCode": self.code,
                "message": str(self), **self.details}


def require(condition, code, message, exit_code=2):
    if not condition:
        raise Error(code, message, exit_code)
