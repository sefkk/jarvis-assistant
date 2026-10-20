from jarvis import exceptions as ex


def test_hierarchy():
    for cls in (ex.ToolValidationError, ex.ToolExecutionError, ex.ToolBlockedError, ex.ToolDeniedError):
        assert issubclass(cls, ex.ToolError)
    for cls in (ex.ConfigError, ex.JarvisAPIError, ex.BudgetExceededError, ex.ToolError, ex.PermissionMissingError):
        assert issubclass(cls, ex.JarvisError)
    assert not issubclass(ex.ConfigError, ex.ToolError)


def test_api_error_one_line():
    err = ex.JarvisAPIError("overloaded", status_code=529, error_type="overloaded_error", request_id="req_1", attempts=3)
    line = err.one_line()
    assert "3 attempt" in line and "529" in line and "overloaded_error" in line and "req_1" in line
    assert ex.JarvisAPIError("x").one_line() == "API call failed after 1 attempt(s)"
