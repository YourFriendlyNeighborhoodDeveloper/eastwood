class EastwoodError(Exception):
    pass

class ConfigError(EastwoodError):
    pass

class ValidationError(EastwoodError):
    pass

class SuppressedError(EastwoodError):
    pass