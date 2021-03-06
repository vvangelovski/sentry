from flask import abort

def get_object_or_404(Model, **kwargs):
    try:
        return Model.objects.get(**kwargs)
    except Model.DoesNotExist:
        abort(404)