# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2024 Bardia Moshiri <fakeshell@bardia.tech>

from os.path import join, exists, splitext
from requests import get
from array import array
from uuid import uuid4
from os import listdir
from re import sub, compile
import asyncio

from dbus_next.service import ServiceInterface, method, dbus_property, signal
from dbus_next.constants import PropertyAccess
from dbus_next import Variant, DBusError

from mmsd.logging import mmsd_print

from mmsdecoder.message import MMSMessage

class OfonoPushNotification(ServiceInterface):
    def __init__(self, bus, ofono_client, ofono_props, ofono_interfaces, ofono_interface_props, mms_dir, export_mms_message, verbose=False):
        super().__init__("org.ofono.PushNotificationAgent")
        self.bus = bus
        self.verbose = verbose
        self.ofono_client = ofono_client
        self.verbose = verbose
        self.ofono_props = ofono_props
        self.ofono_interfaces = ofono_interfaces
        self.ofono_interface_props = ofono_interface_props
        self.mms_dir = mms_dir
        self.export_mms_message = export_mms_message
        self.agent_path = False
        self.registered = False

    async def RegisterAgent(self, path: 'o'):
        if self.registered:
            mmsd_print(f"Agent already registered at path {path}", self.verbose)
            return

        await self.ofono_interfaces['org.ofono.PushNotification'].call_register_agent(path)
        self.bus.export(path, self)

        self.agent_path = path
        self.registered = True
        mmsd_print(f"Agent Registered at path {path}", self.verbose)

    async def UnregisterAgent(self, path: 'o'):
        if not self.registered:
            mmsd_print(f"Agent not registered at path {path}", self.verbose)
            return

        await self.ofono_interfaces['org.ofono.PushNotification'].call_unregister_agent(path)

        self.agent_path = False
        self.registered = False
        mmsd_print(f"Agent Unregistered at path {path}", self.verbose)

    @method()
    async def ReceiveNotification(self, notification: 'ay', info: 'a{sv}'):
        data = array("B", notification)
        mms = MMSMessage.from_data(data)
        for key, value in mms.headers.items():
            if isinstance(value, str):
                mms.headers[key] = sub(r'[^\x20-\x7E]+', '', value).replace('"', '')

        mmsd_print(f"Received MMS: {mms.headers}, info: {info}", self.verbose)

        transaction_id = mms.headers.get('Transaction-Id')
        content_location = mms.headers.get('Content-Location')
        sender = mms.headers.get('From')

        mmsd_print(f"Transaction-Id: {transaction_id}, Content-Location: {content_location}, From: {sender}", self.verbose)

        proxy = ''
        proxy = await self.get_mms_context_info()
        if proxy != '':
            proxies = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}
            try:
                response = get(content_location, proxies=proxies)
                if response.status_code == 200:
                    uuid = str(uuid4()).replace('-', '1')
                    smil_path = join(self.mms_dir, uuid)
                    status_path = join(self.mms_dir, f"{uuid}.status")
                    headers_path = join(self.mms_dir, f"{uuid}.headers")

                    with open(smil_path, 'wb') as file:
                        file.write(response.content)
                        mmsd_print(f"SMIL successfully saved to {smil_path}", self.verbose)

                    mms_smil = MMSMessage.from_data(response.content)

                    with open(headers_path, 'w') as headers_file:
                        for header_key, header_value in mms_smil.headers.items():
                            headers_file.write(f"{header_key}={header_value}\n")
                        mmsd_print(f"Headers successfully saved to {headers_path}", self.verbose)

                    sent_time = info['SentTime'].value if 'SentTime' in info else ''
                    message_id = mms_smil.headers.get('Transaction-Id') or mms_smil.headers.get('Message-ID') or transaction_id or ''

                    meta_info = f"""[info]
read=false
state=received
id={message_id}
date={sent_time}"""

                    with open(status_path, 'w') as status_file:
                        status_file.write(meta_info)
                        mmsd_print(f"Meta info successfully saved to {status_path}", self.verbose)

                    attachments = []
                    smil_src = len(mms_smil.data_parts)
                    for index, part in enumerate(mms_smil.data_parts):
                        attachment_path = join(self.mms_dir, f"{uuid}.attachment.{index}")
                        with open(attachment_path, 'wb') as file:
                            mmsd_print(f"Writing attachment with index {index} of content type {part.content_type} to {attachment_path}", self.verbose)
                            file.write(part.data)

                        if 'application/smil' in part.content_type:
                            # smil should be added to smil prop only, not to attachments
                            smil_data = part.data.decode('utf-8').replace("\n", "").replace("\r", "")
                            smil_src = self.extract_smil_src(smil_data)
                            if smil_src is None:
                                num_attachments = len(mms_smil.data_parts)
                        else:
                            attachment_info = [f'<{smil_src[index-1]}>', part.content_type, attachment_path, 0, len(part.data)]
                            attachments.append(attachment_info)

                    if smil_data:
                        recipients = [] # need a way to check if its a group, then query for recipients
                        sender_number = sender.split('/')[0]
                        self.export_mms_message(uuid, 'received', sent_time, sender_number, mms_smil.headers.get('Delivery-Report'), recipients, smil_data, attachments)
                else:
                    mmsd_print(f"Failed to retrieve SMIL. Status code: {response.status_code}", self.verbose)
            except Exception as e:
                mmsd_print(f"Failed to download SMIL: {e}", self.verbose)
                pass # we should handle messages that could not be fetched from message center
        else:
            mmsd_print(f"Could not pull down the mms message. proxy is empty", self.verbose)

    @method()
    async def Release(self):
        mmsd_print(f"Agent released on path {self.agent_path}", self.verbose)
        self.registered = False
        self.bus.unexport(self.agent_path)

    def export_old_messages(self):
        export_entries = {}

        for filename in listdir(self.mms_dir):
            if filename.endswith('.status'):
                basename = filename.rsplit('.status', 1)[0]
                attachment_count = 0
                headers_file = basename + '.headers'

                if exists(join(self.mms_dir, headers_file)):
                    for attachment_file in listdir(self.mms_dir):
                        if attachment_file.startswith(basename + '.attachment.'):
                            attachment_count += 1

                    if attachment_count >= 2:
                        status_path = join(self.mms_dir, filename)

                        if exists(status_path):
                            status_data = {
                                'state': None,
                                'date': None,
                                'sender': None,
                                'delivery_report': None,
                                'smil_data': None,
                                'attachments': []
                            }

                            with open(status_path, 'r') as file:
                                for line in file:
                                    if line.startswith('state='):
                                        status_data['state'] = line.split('=')[1].strip()
                                    elif line.startswith('date='):
                                        status_data['date'] = line.split('=')[1].strip()

                            smil_file = join(self.mms_dir, basename)
                            if exists(smil_file):
                                with open(smil_file, 'rb') as smil_data:
                                    mms_smil = MMSMessage.from_data(smil_data.read())

                                    if mms_smil.headers['Delivery-Report']:
                                        status_data['delivery_report'] = mms_smil.headers['Delivery-Report']
                                    else:
                                        status_data['delivery_report'] = False

                                    if mms_smil.headers['From']:
                                        status_data['sender'] = mms_smil.headers['From'].split('/')[0]
                                    else:
                                        status_data['sender'] = ''

                                    smil_src = len(mms_smil.data_parts)
                                    for index, part in enumerate(mms_smil.data_parts):
                                        attachment_path = join(self.mms_dir, f"{basename}.attachment.{index}")

                                        if 'application/smil' in part.content_type:
                                            # smil should be added to smil prop only, not to attachments
                                            smil_data = part.data.decode('utf-8').replace("\n", "").replace("\r", "")
                                            status_data['smil_data'] = smil_data
                                            smil_src = self.extract_smil_src(smil_data)
                                            if smil_src is None:
                                                num_attachments = len(mms_smil.data_parts)
                                        else:
                                            attachment_info = [f'<{smil_src[index-1]}>', part.content_type, attachment_path, 0, len(part.data)]
                                            status_data['attachments'].append(attachment_info)

                            export_entries[basename] = status_data

        for basename, entry in export_entries.items():
            mmsd_print(f"re-exporting old message {basename}", self.verbose)
            self.export_mms_message(basename, entry['state'], entry['date'], entry['sender'], entry['delivery_report'], [], entry['smil_data'], entry['attachments'])

    async def get_mms_context_info(self):
        proxy = ''
        if 'org.ofono.ConnectionManager' in self.ofono_interfaces:
            contexts = await self.ofono_interfaces['org.ofono.ConnectionManager'].call_get_contexts()
            for ctx in contexts:
                name = ctx[1].get('Type', Variant('s', '')).value
                if name.lower() == "mms":
                    proxy = ctx[1]['MessageProxy'].value
        return proxy

    def extract_smil_src(self, xml_string):
        src_pattern = compile(r'<(\w+)\s+[^>]*src="([^"]*)"[^>]*>')
        sources = src_pattern.findall(xml_string)
        src_list = [splitext(match[1])[0] for match in sources]
        if src_list:
            return src_list
        else:
            return None

    def ofono_changed(self, name, varval):
        self.ofono_props[name] = varval

    def ofono_client_changed(self, ofono_client):
        self.ofono_client = ofono_client

    def ofono_interface_changed(self, iface):
        def ch(name, varval):
            if iface in self.ofono_interface_props:
                self.ofono_interface_props[iface][name] = varval

        return ch
