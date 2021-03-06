import json

from collections import defaultdict

from django.db import connection
from django.http import HttpResponse, Http404
from django.shortcuts import get_object_or_404

from catmaid.models import Project, Class, ClassInstance, Relation, Connector, \
        ConnectorClassInstance, UserRole, Treenode, TreenodeClassInstance, \
        ChangeRequest
from catmaid.control.authentication import requires_user_role, can_edit_or_fail
from catmaid.fields import Double3D

@requires_user_role([UserRole.Annotate, UserRole.Browse])
def label_remove(request, project_id=None):
    # check if superuser, then delete label and all associated instances
    class_instance_for_label = int(request.POST['class_instance_id'])
    if request.user.is_superuser:
        ClassInstance.objects.filter(id=class_instance_for_label).delete()
        return HttpResponse(json.dumps({'message': 'success'}), content_type="text/plain")
    return HttpResponse(json.dumps({}), content_type="text/plain")

@requires_user_role([UserRole.Annotate, UserRole.Browse])
def labels_all(request, project_id=None):
    qs = ClassInstance.objects.filter(
        class_column__class_name='label',
        project=project_id)
    return HttpResponse(json.dumps(list(x.name for x in qs)), content_type="text/plain")

@requires_user_role([UserRole.Annotate, UserRole.Browse])
def labels_for_node(request, project_id=None, ntype=None, location_id=None):
    if ntype == 'treenode':
        qs = TreenodeClassInstance.objects.filter(
            relation__relation_name='labeled_as',
            class_instance__class_column__class_name='label',
            treenode=location_id,
            project=project_id).select_related('class_instance__name')
    elif ntype == 'location' or ntype == 'connector':
        qs = ConnectorClassInstance.objects.filter(
            relation__relation_name='labeled_as',
            class_instance__class_column__class_name='label',
            connector=location_id,
            project=project_id).select_related('class_instance__name')
    else:
        raise Http404('Unknown node type: "%s"' % (ntype,))
    return HttpResponse(json.dumps(list(x.class_instance.name for x in qs)), content_type="text/plain")

@requires_user_role([UserRole.Annotate, UserRole.Browse])
def labels_for_nodes(request, project_id=None):
    # Two POST variables, which are each an array of integers stringed together
    # with commas as separators
    treenode_ids = request.POST.get('treenode_ids', '').strip()
    connector_ids = request.POST.get('connector_ids', '').strip()
    result = defaultdict(list)
    cursor = connection.cursor();

    if treenode_ids:
        # Could use treenode_ids directly as a string, but it is good to sanitize arguments
        cursor.execute('''
        SELECT treenode.id, class_instance.name
        FROM treenode, class_instance, treenode_class_instance, relation
        WHERE relation.id = treenode_class_instance.relation_id
          AND relation.relation_name = 'labeled_as'
          AND treenode_class_instance.treenode_id = treenode.id
          AND class_instance.id = treenode_class_instance.class_instance_id
          AND treenode.id IN (%s)
        ''' % ','.join(str(int(x)) for x in treenode_ids.split(','))) # convoluted to sanitize

        for row in cursor.fetchall():
            result[row[0]].append(row[1])
        # The code below:
        # 1. Is hard to read, compared to plain SQL (see above)
        # 2. Selects all possible columns, wastefully
        # 3. If appended with values(...), then returns a dictionary, wastefully
        # 4. Runs slower than the equivalent code above
        """
        qs_treenodes = TreenodeClassInstance.objects.filter(
            relation__relation_name='labeled_as',
            class_instance__class_column__class_name='label',
            treenode__id__in=(int(x) for x in treenode_ids.split(',')),
            project=project_id).select_related('treenode', 'class_instance').values('treenode_id', 'class_instance__name')
        for tci in qs_treenodes:
            result[tci['treenode_id']].append(tci['class_instance__name'])
        """

    if connector_ids:
        cursor.execute('''
        SELECT connector.id, class_instance.name
        FROM connector, class_instance, connector_class_instance, relation
        WHERE relation.id = connector_class_instance.relation_id
          AND relation.relation_name = 'labeled_as'
          AND connector_class_instance.connector_id = connector.id
          AND class_instance.id = connector_class_instance.class_instance_id
          AND connector.id IN (%s)
        ''' % ','.join(str(int(x)) for x in connector_ids.split(','))) # convoluted to sanitize
        for row in cursor.fetchall():
            result[row[0]].append(row[1])

        # See notes above for treenode_ids
        """
        qs_connectors = ConnectorClassInstance.objects.filter(
            relation__relation_name='labeled_as',
            class_instance__class_column__class_name='label',
            connector__id__in=(int(x) for x in connector_ids.split(',')),
            project=project_id).select_related('connector', 'class_instance')
        for cci in qs_connectors:
            result[cci.connector.id].append(cci.class_instance.name)
        """

    return HttpResponse(json.dumps(result), content_type="text/plain")

@requires_user_role(UserRole.Annotate)
def label_update(request, project_id=None, location_id=None, ntype=None):
    """ location_id is the ID of a treenode or connector.
        ntype is either 'treenode' or 'connector'. """
    labeled_as_relation = Relation.objects.get(project=project_id, relation_name='labeled_as')
    p = get_object_or_404(Project, pk=project_id)

    # TODO will FAIL when a tag contains a coma by itself
    new_tags = request.POST['tags'].split(',')

    kwargs = {'relation': labeled_as_relation,
              'class_instance__class_column__class_name': 'label'}

    if 'treenode' == ntype:
        table = TreenodeClassInstance
        kwargs['treenode__id'] = location_id
        node = Treenode.objects.get(id=location_id)
    elif 'connector' == ntype:
        table = ConnectorClassInstance
        kwargs['connector__id'] = location_id
        node = Connector.objects.get(id=location_id)

    if not table:
        raise Http404('Unknown node type: "%s"' % (ntype,))

    # Get the existing list of tags for the tree node/connector and delete any that are not in the new list.
    existingLabels = table.objects.filter(**kwargs).select_related('class_instance__name')
    labels_to_delete = table.objects.filter(**kwargs).exclude(class_instance__name__in=new_tags)

    # Iterate over all labels that should get deleted to check permission
    # on each one. Remember each label that couldn't be deleted in the
    # other_labels array.
    other_labels = []
    deleted_labels = []
    for l in labels_to_delete:
        try:
            can_edit_or_fail(request.user, l.id, table._meta.db_table)
            if remove_label(l.id, ntype):
                deleted_labels.append(l)
            else:
                other_labels.append(l)
        except:
            other_labels.append(l)

    # Create change requests for labels associated to the treenode by other users
    for label in other_labels:
        ChangeRequest(**{'type': 'Remove Tag',
                       'project': p,
                       'user': request.user,
                       'recipient': node.user,
                       'location': Double3D(node.location_x, node.location_y, node.location_z),
                       ntype: node,
                       'description': 'Remove tag \'' + label.class_instance.name + '\'',
                       'validate_action': 'from catmaid.control.label import label_exists\nis_valid = label_exists(' + str(label.id) + ', "' + ntype + '")',
                       'approve_action': 'from catmaid.control.label import remove_label\nremove_label(' + str(label.id) + ', "' + ntype + '")'}).save()

    existing_names = set(ele.class_instance.name for ele in existingLabels)

    # Add any new labels.
    label_class = Class.objects.get(project=project_id, class_name='label')
    kwargs = {'user': request.user,
              'project': p,
              'relation': labeled_as_relation,
              ntype: node}

    for tag_name in new_tags:
        if len(tag_name) > 0 and tag_name not in existing_names:
            # Make sure the tag instance exists
            existing_tags = tuple(ClassInstance.objects.filter(
                project=p,
                name=tag_name,
                class_column=label_class))
            if len(existing_tags) < 1:
                tag = ClassInstance(
                    project=p,
                    name=tag_name,
                    user=request.user,
                    class_column=label_class)
                tag.save()
            else:
                tag = existing_tags[0]

            # Associate the tag with the treenode/connector.
            kwargs['class_instance'] = tag
            tci = table(**kwargs) # creates new TreenodeClassInstance or ConnectorClassInstance
            tci.save()

            if node.user != request.user:
                # Inform the owner of the node that the tag was added and give them the option of removing it.
                ChangeRequest(**{'type': 'Add Tag', 
                               'description': 'Added tag \'' + tag_name + '\'', 
                               'project': p, 
                               'user': request.user,
                               'recipient': node.user,
                               'location': Double3D(node.location_x, node.location_y, node.location_z),
                               'treenode': node,
                               'validate_action': 'from catmaid.control.label import label_exists\nis_valid = label_exists(' + str(tci.id) + ', "' + ntype + '")',
                              'reject_action': 'from catmaid.control.label import remove_label\nremove_label(' + str(tci.id) + ', "' + ntype + '")'}).save()


    return HttpResponse(json.dumps({'message': 'success'}), content_type='text/json')


def label_exists(label_id, node_type):
    # This checks to see if the exact instance of the tag being applied to a node/connector still exists.
    # If the tag was removed and added again then this will return False.
    if node_type == 'treenode':
        try:
            label = TreenodeClassInstance.objects.get(pk=label_id)
            return True
        except TreenodeClassInstance.DoesNotExist:
            return False
    elif node_type == 'connector':
        try:
            label = ConnectorClassInstance.objects.get(pk=label_id)
            return True
        except ConnectorClassInstance.DoesNotExist:
            return False
    else:
        raise Exception('Unknown node type: "%s"', node_type)


def remove_label(label_id, node_type):
    # This removes an exact instance of a tag being applied to a node/connector, it does not look up the tag by name.
    # If the tag was removed and added again then this will do nothing and the tag will remain.
    if node_type == 'treenode':
        table = TreenodeClassInstance
    elif node_type == 'connector':
        table = ConnectorClassInstance
    else:
        raise Exception('Unknown node type: "%s"', node_type)

    try:
        label_link = table.objects.get(pk=label_id)
        label = label_link.class_instance
        label_link.delete()
        # Remove class instance for all deleted labels, if it isn't linked to any
        # treenode anymore.
        if 0 == table.objects.filter(class_instance=label).count():
            label.delete()

        return True
    except table.DoesNotExist:
        return False
