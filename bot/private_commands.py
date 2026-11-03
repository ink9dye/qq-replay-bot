"""Optional locally installed command extension; never part of public help."""
import importlib.util
import threading

_LOAD_LOCK=threading.Lock()


def dispatch(service, user_id, text, conversation):
    with _LOAD_LOCK:
        if not hasattr(service,'_private_command_handler'):
            path=service.data_dir/'private-commands'/'handler.py'
            handler=None
            if path.is_file():
                spec=importlib.util.spec_from_file_location('_local_private_commands',path)
                module=importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                handler=module.handle
            service._private_command_handler=handler
    if service._private_command_handler is None:return False,None
    return service._private_command_handler(service,str(user_id),text,conversation)
